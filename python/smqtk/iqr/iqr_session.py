import io
import json
import logging
import threading
import uuid
import zipfile

import six

from smqtk.algorithms.relevancy_index import RelevancyIndex
from smqtk.representation.descriptor_set.memory import MemoryDescriptorSet
from smqtk.utils import SmqtkObject
from smqtk.utils.configuration import from_config_dict


DFLT_REL_INDEX_CONFIG = {
    "type": "LibSvmHikRelevancyIndex",
    "LibSvmHikRelevancyIndex": {
        "descr_cache_filepath": None,
    }
}


class IqrSession (SmqtkObject):
    """
    Encapsulation of IQR Session related data structures with a centralized
    lock for multi-thread access.

    This object is compatible with the python with-statement, so when elements
    are to be used or modified, it should be within a with-block so race
    conditions do not occur across threads/sub-processes.

    """

    @property
    def _log(self):
        return logging.getLogger(
            '.'.join((self.__module__, self.__class__.__name__)) +
            "[%s]" % self.uuid
        )

    def __init__(self, pos_seed_neighbors=500,
                 rel_index_config=None, session_uid=None):
        """
        Initialize the IQR session

        This does not initialize the working set for ranking as there are no
        known positive descriptor examples at this time.

        Adjudications
        -------------
        Adjudications are carried through between initializations. This allows
        indexed material adjudicated through-out the lifetime of the session to
        stay relevant.

        :param pos_seed_neighbors: Number of neighbors to pull from the given
            ``nn_index`` for each positive exemplar when populating the working
            set, i.e. this value determines the size of the working set for
            IQR refinement. By default, we try to get 500 neighbors.

            Since there may be partial to significant overlap of near neighbors
            as a result of nn_index queries for positive exemplars, the working
            set may contain anywhere from this value's number of entries, to
            ``N*P``, where ``N`` is this value and ``P`` is the number of
            positive examples at the time of working set initialization.
        :type pos_seed_neighbors: int

        :param rel_index_config: Plugin configuration dictionary for the
            RelevancyIndex to use for ranking user adjudications. If `None` we
            default to using an in-memory libSVM based index using the histogram
            intersection metric.
        :type rel_index_config: None | dict

        :param session_uid: Optional manual specification of session UUID. By
            default this will be a string UUID as generated by
            ``uuid.uuid1()``.
        :type session_uid: str | uuid.UUID

        """
        self.uuid = session_uid or str(uuid.uuid1()).replace('-', '')
        self.lock = threading.RLock()

        self.pos_seed_neighbors = int(pos_seed_neighbors)

        # Local descriptor set for ranking, populated by a query to the
        #   nn_index instance.
        # Added external data/descriptors not added to this set.
        self.working_set = MemoryDescriptorSet()

        # Book-keeping set so we know what positive descriptors
        # UUIDs we've used to query the neighbor index with already.
        #: :type: set[collections.abc.Hashable]
        self._wi_seeds_used = set()

        # Descriptor elements representing data from external sources.
        # These may be arbitrary descriptor elements not present in
        #   ``working_index``.
        #: :type: set[smqtk.representation.DescriptorElement]
        self.external_positive_descriptors = set()
        #: :type: set[smqtk.representation.DescriptorElement]
        self.external_negative_descriptors = set()

        # Descriptor references from ``working_set`` that have been
        #   adjudicated.
        # These should be sub-sets of the descriptors contained in the
        #   ``working_set``.
        #: :type: set[smqtk.representation.DescriptorElement]
        self.positive_descriptors = set()
        #: :type: set[smqtk.representation.DescriptorElement]
        self.negative_descriptors = set()

        # Sets of descriptor elements that were used in the last refinement
        #   to achieve the currently cached results, i.e. "contributed" to the
        #   current results state.
        # These sets are empty before the first refine after construction or a
        #   reset.
        #: :type: set[smqtk.representation.DescriptorElement]
        self.rank_contrib_pos = set()
        #: :type: set[smqtk.representation.DescriptorElement]
        self.rank_contrib_pos_ext = set()
        #: :type: set[smqtk.representation.DescriptorElement]
        self.rank_contrib_neg = set()
        #: :type: set[smqtk.representation.DescriptorElement]
        self.rank_contrib_neg_ext = set()

        # Mapping of a DescriptorElement in our relevancy search index (not the
        #   set that the nn_index uses) to the relevancy score given the
        #   recorded positive and negative adjudications.
        # This is None before any initialization or refinement occurs.
        #: :type: None | dict[smqtk.representation.DescriptorElement, float]
        self.results = None

        # Cache variables for views of refinement results.
        # All results as a list in order of relevancy score.
        #: :type: None | list[(smqtk.representation.DescriptorElement, float)]
        self._ordered_results = None
        #: Positively adjudicated descriptors in order of relevancy score.
        #: :type: None | list[(smqtk.representation.DescriptorElement, float)]
        self._ordered_pos = None
        # Negatively adjudicated descriptors in order of relevancy score.
        #: :type: None | list[(smqtk.representation.DescriptorElement, float)]
        self._ordered_neg = None
        # Non-adjudicated descriptors in our working set in order of
        # relevancy score.
        #: :type: None | list[(smqtk.representation.DescriptorElement, float)]
        self._ordered_non_adj = None

        #
        # Algorithm Instances [+Config]
        #
        # RelevancyIndex configuration and instance that is used for producing
        #   results.
        # This is only [re]constructed when initializing the session.
        if rel_index_config is None:
            rel_index_config = DFLT_REL_INDEX_CONFIG
        self.rel_index_config = rel_index_config
        # This is None until session initialization happens after pos/neg
        # exemplar data has been added.
        #: :type: None | smqtk.algorithms.relevancy_index.RelevancyIndex
        self.rel_index = None

    def __enter__(self):
        """
        :rtype: IqrSession
        """
        self.lock.acquire()
        return self

    # noinspection PyUnusedLocal
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.lock.release()

    def external_descriptors(self, positive=(), negative=()):
        """
        Add positive/negative descriptors from external data.

        These descriptors may not be a part of our working set.

        TODO: Add ability to "remove" positive/negative external descriptors.
              See ``adjudicate`` method "un_..." parameters.

        :param positive: Iterable of descriptors from external sources to
            consider positive examples.
        :type positive:
            collections.abc.Iterable[smqtk.representation.DescriptorElement]

        :param negative: Iterable of descriptors from external sources to
            consider negative examples.
        :type negative:
            collections.abc.Iterable[smqtk.representation.DescriptorElement]

        """
        positive = set(positive)
        negative = set(negative)
        with self.lock:
            self.external_positive_descriptors.update(positive)
            self.external_positive_descriptors.difference_update(negative)

            self.external_negative_descriptors.update(negative)
            self.external_negative_descriptors.difference_update(positive)

    def adjudicate(self, new_positives=(), new_negatives=(),
                   un_positives=(), un_negatives=()):
        """
        Update current state of working set positive and negative
        adjudications based on descriptor UUIDs.

        If the same descriptor element is listed in both new positives and
        negatives, they cancel each other out, causing that descriptor to not
        be included in the adjudication.

        The given iterables must be re-traversable. Otherwise the given
        descriptors will not be properly registered.

        :param new_positives: Descriptors of elements in our working set to
            now be considered to be positively relevant.
        :type new_positives:
            collections.abc.Iterable[smqtk.representation.DescriptorElement]

        :param new_negatives: Descriptors of elements in our working set to
            now be considered to be negatively relevant.
        :type new_negatives:
            collections.abc.Iterable[smqtk.representation.DescriptorElement]

        :param un_positives: Descriptors of elements in our working set to now
            be considered not positive any more.
        :type un_positives:
            collections.abc.Iterable[smqtk.representation.DescriptorElement]

        :param un_negatives: Descriptors of elements in our working set to now
            be considered not negative any more.
        :type un_negatives:
            collections.abc.Iterable[smqtk.representation.DescriptorElement]

        """
        # TODO: Assert that inputs are indeed in the working set?

        new_positives = set(new_positives)
        new_negatives = set(new_negatives)
        un_positives = set(un_positives)
        un_negatives = set(un_negatives)

        with self.lock:
            pos_before = set(self.positive_descriptors)
            self.positive_descriptors.update(new_positives)
            self.positive_descriptors.difference_update(un_positives)
            self.positive_descriptors.difference_update(new_negatives)
            pos_changed = pos_before != self.positive_descriptors
            if pos_changed:
                # Reset ordered positives cache if pos adjudications changed.
                self._ordered_pos = None

            neg_before = set(self.negative_descriptors)
            self.negative_descriptors.update(new_negatives)
            self.negative_descriptors.difference_update(un_negatives)
            self.negative_descriptors.difference_update(new_positives)
            neg_changed = neg_before != self.negative_descriptors
            if neg_changed:
                # Reset ordered negatives cache if neg adjudications changed.
                self._ordered_neg = None

            if pos_changed or neg_changed:
                # Reset non-adjudicated cache if anything changed.
                self._ordered_non_adj = None

    def update_working_set(self, nn_index):
        """
        Initialize or update our current working set using the given
        :class:`.NearestNeighborsIndex` instance given our current positively
        labeled descriptor elements.

        We only query from the index for new positive elements since the last
        update or reset.

        :param nn_index: :class:`.NearestNeighborsIndex` to query from.
        :type nn_index: smqtk.algorithms.NearestNeighborsIndex

        :raises RuntimeError: There are no positive example descriptors in this
            session to use as a basis for querying.

        """
        pos_examples = (self.external_positive_descriptors |
                        self.positive_descriptors)
        if len(pos_examples) == 0:
            raise RuntimeError("No positive descriptors to query the neighbor "
                               "index with.")

        # Not clearing working set because this step is intended to be
        # additive.
        updated = False

        # adding to working set
        self._log.info("Building working set using %d positive examples "
                       "(%d external, %d adjudicated)",
                       len(pos_examples),
                       len(self.external_positive_descriptors),
                       len(self.positive_descriptors))
        # TODO: parallel_map and reduce with merge-dict
        for p in pos_examples:
            if p.uuid() not in self._wi_seeds_used:
                self._log.debug("Querying neighbors to: %s", p)
                self.working_set.add_many_descriptors(
                    nn_index.nn(p, n=self.pos_seed_neighbors)[0]
                )
                self._wi_seeds_used.add(p.uuid())
                updated = True

        # Make new relevancy index
        if updated:
            self._log.info("Creating new relevancy index over working set.")
            #: :type: smqtk.algorithms.relevancy_index.RelevancyIndex
            self.rel_index = from_config_dict(
                self.rel_index_config, RelevancyIndex.get_impls()
            )
            self.rel_index.build_index(self.working_set.iterdescriptors())

    def refine(self):
        """ Refine current model results based on current adjudication state

        :raises RuntimeError: No working set has been initialized.
            :meth:`update_working_set` should have been called after
            adjudicating some positive examples.
        :raises RuntimeError: There are no adjudications to run on. We must
            have at least one positive adjudication.

        """
        with self.lock:
            if not self.rel_index:
                raise RuntimeError("No relevancy index yet. Must not have "
                                   "initialized session (no working set).")

            # combine pos/neg adjudications + added external data descriptors
            pos = self.positive_descriptors | self.external_positive_descriptors
            neg = self.negative_descriptors | self.external_negative_descriptors

            if not pos:
                raise RuntimeError("Did not find at least one positive "
                                   "adjudication.")

            self._log.debug("Ranking working set with %d pos and %d neg total "
                            "examples.", len(pos), len(neg))
            element_probability_map = self.rel_index.rank(pos, neg)
            self.results = element_probability_map
            # Record UIDs of elements used for relevancy ranking.
            # - shallow copy for separate container instance
            self.rank_contrib_pos = set(self.positive_descriptors)
            self.rank_contrib_pos_ext = set(self.external_positive_descriptors)
            self.rank_contrib_neg = set(self.negative_descriptors)
            self.rank_contrib_neg_ext = set(self.external_negative_descriptors)
            # Clear result view caches
            self._ordered_results = self._ordered_pos = self._ordered_neg = \
                self._ordered_non_adj = None

    def ordered_results(self):
        """
        Return a tuple of all working-set descriptor elements as tuples of
        ``(element, score)`` in order of descending relevancy score.

        If refinement has not yet occurred since session creation or the last
        reset, an empty tuple is returned.

        :rtype: None | tuple[(smqtk.representation.DescriptorElement, float)]
        """
        with self.lock:
            try:
                return list(self._ordered_results)
            except TypeError:
                # NoneType is not iterable
                # Cache did non exist.

                try:
                    result_items = six.iteritems(self.results)
                except AttributeError:
                    # NoneType missing items/iteritems attr
                    # No results to iterate over.
                    return list()

                r = self._ordered_results = sorted(
                    result_items,
                    key=lambda p: p[1], reverse=True
                )
                # Shallow copy of the list to protect against external mutation
                return list(r)

    def get_positive_adjudication_relevancy(self):
        """
        Return a list of the positively adjudicated descriptors as tuples of
        ``(element, score)`` in order of descending relevancy score.

        This does *not* include external positive adjudications, only
        positively adjudicated descriptors in the working set.

        If refinement has not yet occurred since session creation or the last
        reset, an empty list is returned.

        Cache is invalidated when:
        - A refinement occurs.
        - Positive adjudications change.

        :rtype: None | list[(smqtk.representation.DescriptorElement, float)]
        """
        with self.lock:
            try:
                return list(self._ordered_pos)
            except TypeError:
                # NoneType is not iterable
                # No cache yet.

                rank_contrib_pos = \
                    self.rank_contrib_pos | self.rank_contrib_pos_ext
                # Results already ordered, so only filter
                r = self._ordered_pos = list(
                    filter(lambda t: t[0] in rank_contrib_pos,
                           self.ordered_results())
                )
                # Shallow copy of the list to protect against external mutation
                return list(r)

    def get_negative_adjudication_relevancy(self):
        """
        Return a list of the negatively adjudicated descriptors as tuples of
        ``(element, score)`` in order of descending relevancy score.

        This does *not* include external negative adjudications, only
        negatively adjudicated descriptors in the working set.

        If refinement has not yet occurred since session creation or the last
        reset, an empty list is returned.

        Cache is invalidated when:
        - A refinement occurs.
        - Negative adjudications change.

        :rtype: None | list[(smqtk.representation.DescriptorElement, float)]
        """
        with self.lock:
            try:
                return list(self._ordered_neg)
            except TypeError:
                # NoneType is not iterable
                # No cache yet.

                rank_contrib_neg = \
                    self.rank_contrib_neg | self.rank_contrib_neg_ext
                # Results already ordered, so only filter
                r = self._ordered_neg = list(
                    filter(lambda t: t[0] in rank_contrib_neg,
                           self.ordered_results())
                )
                # Shallow copy of the list to protect against external mutation
                return list(r)

    def get_unadjudicated_relevancy(self):
        """
        Return a list of the non-adjudicated descriptor elements as tuples of
        ``(element, score)`` in order of descending relevancy score.

        If refinement has not yet occurred since session creation or the last
        reset, an empty list is returned.

        :rtype: None | list[(smqtk.representation.DescriptorElement, float)]
        """
        with self.lock:
            try:
                return list(self._ordered_non_adj)
            except TypeError:
                # NoneType is not iterable
                # No cache yet
                pos_and_neg = \
                    self.rank_contrib_pos | self.rank_contrib_pos_ext | \
                    self.rank_contrib_neg | self.rank_contrib_neg_ext

                # Results already ordered, so only filter
                r = self._ordered_non_adj = list(
                    filter(lambda t: t[0] not in pos_and_neg,
                           self.ordered_results())
                )
                # Shallow copy of the list to protect against external mutation
                return list(r)

    def reset(self):
        """ Reset the IQR Search state

        No positive adjudications, reload original feature data

        """
        with self.lock:
            self.working_set.clear()
            self._wi_seeds_used.clear()
            self.positive_descriptors.clear()
            self.negative_descriptors.clear()
            self.external_positive_descriptors.clear()
            self.external_negative_descriptors.clear()
            self.rank_contrib_pos.clear()
            self.rank_contrib_pos_ext.clear()
            self.rank_contrib_neg.clear()
            self.rank_contrib_neg_ext.clear()

            self.rel_index = None
            self.results = None
            self._ordered_results = self._ordered_pos = self._ordered_neg = \
                self._ordered_non_adj = None

    ###########################################################################
    # I/O Methods

    # I/O Constants. These should not be changed.
    STATE_ZIP_COMPRESSION = zipfile.ZIP_DEFLATED
    STATE_ZIP_FILENAME = "iqr_state.json"

    def get_state_bytes(self):
        """
        Get a byte representation of the current descriptor and adjudication
        state of this session.

        This does not encode current results or the relevancy index's state, but
        these can be reproduced with this state.

        :return: State representation bytes
        :rtype: bytes

        """
        def d_set_to_list(d_set):
            # Convert set of descriptors to list of tuples:
            #   [..., (uuid, type, vector), ...]
            return [(d.uuid(), d.type(), d.vector().tolist()) for d in d_set]

        with self:
            # Convert session descriptors into basic values.
            pos_d = d_set_to_list(self.positive_descriptors)
            neg_d = d_set_to_list(self.negative_descriptors)
            ext_pos_d = d_set_to_list(self.external_positive_descriptors)
            ext_neg_d = d_set_to_list(self.external_negative_descriptors)

        z_buffer = io.BytesIO()
        z = zipfile.ZipFile(z_buffer, 'w', self.STATE_ZIP_COMPRESSION)
        z.writestr(self.STATE_ZIP_FILENAME, json.dumps({
            'pos': pos_d,
            'neg': neg_d,
            'external_pos': ext_pos_d,
            'external_neg': ext_neg_d,
        }))
        z.close()
        return z_buffer.getvalue()

    def set_state_bytes(self, b, descriptor_factory):
        """
        Set this session's state to the given byte representation, resetting
        this session in the process.

        Bytes given must have been retrieved via a previous call to
        ``get_state_bytes`` otherwise this method will fail.

        Since this state may be completely different from the current state,
        this session is reset before applying the new state. Thus, any current
        ranking results are thrown away.

        :param b: Bytes to set this session's state to.
        :type b: bytes

        :param descriptor_factory: Descriptor element factory to use when
            generating descriptor elements from extracted data.
        :type descriptor_factory: smqtk.representation.DescriptorElementFactory

        :raises ValueError: The input bytes could not be loaded due to
            incompatibility.

        """
        z_buffer = io.BytesIO(b)
        z = zipfile.ZipFile(z_buffer, 'r', self.STATE_ZIP_COMPRESSION)
        if self.STATE_ZIP_FILENAME not in z.namelist():
            raise ValueError("Invalid bytes given, did not contain expected "
                             "zipped file name.")

        # Extract expected json file object
        state = json.loads(z.read(self.STATE_ZIP_FILENAME).decode())
        del z, z_buffer

        with self:
            self.reset()

            def load_descriptor(_uid, _type_str, vec_list):
                _e = descriptor_factory.new_descriptor(_type_str, _uid)
                if _e.has_vector():
                    assert _e.vector().tolist() == vec_list, \
                        "Found existing vector for UUID '%s' but vectors did " \
                        "not match."
                else:
                    _e.set_vector(vec_list)
                return _e

            # Read in raw descriptor data from the state, convert to descriptor
            # element, then store in our descriptor sets.
            for source, target in [(state['external_pos'],
                                    self.external_positive_descriptors),
                                   (state['external_neg'],
                                    self.external_negative_descriptors),
                                   (state['pos'], self.positive_descriptors),
                                   (state['neg'], self.negative_descriptors)]:
                for uid, type_str, vector_list in source:
                    e = load_descriptor(uid, type_str, vector_list)
                    target.add(e)
