from __future__ import division, print_function
import inspect
import os
import pickle
import unittest

import PIL.Image
import mock
import nose.tools
import numpy

from smqtk.algorithms.descriptor_generator import get_descriptor_generator_impls
from smqtk.algorithms.descriptor_generator.pytorch_descriptor import \
     PytorchDescriptorGenerator
from smqtk.representation.data_element import from_uri
from smqtk.representation.data_element.url_element import DataUrlElement
from smqtk.tests import TEST_DATA_DIR

from torchvision import models, transforms


if PytorchDescriptorGenerator.is_usable():

    class TestPytorchDesctriptorGenerator (unittest.TestCase):

        lenna_image_fp = os.path.join(TEST_DATA_DIR, 'Lenna.png')

        # lenna_alexnet_fc7_descr_fp = \
        #     os.path.join(TEST_DATA_DIR, 'Lenna.alexnet_fc7_output.npy')
        #
        # # Dummy Caffe configuration files + weights
        # # - weights is actually an empty file (0 bytes), which caffe treats
        # #   as random/zero values (not sure exactly what's happening, but
        # #   always results in a zero-vector).
        # dummy_net_topo_fp = \
        #     os.path.join(TEST_DATA_DIR, 'caffe.dummpy_network.prototxt')
        # dummy_caffe_model_fp = \
        #     os.path.join(TEST_DATA_DIR, 'caffe.empty_model.caffemodel')
        # dummy_img_mean_fp = \
        #     os.path.join(TEST_DATA_DIR, 'caffe.dummy_mean.npy')
        #
        # www_uri_alexnet_prototxt = \
        #     'https://data.kitware.com/api/v1/file/57e2f3fd8d777f10f26e532c/download'
        # www_uri_alexnet_caffemodel = \
        #     'https://data.kitware.com/api/v1/file/57dae22f8d777f10f26a2a86/download'
        # www_uri_image_mean_proto = \
        #     'https://data.kitware.com/api/v1/file/57dae0a88d777f10f26a2a82/download'

        def setUp(self):
            normalize = transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                             std=[0.229, 0.224, 0.225])
            self.transform = transforms.Compose([
                transforms.Resize(256),
                transforms.CenterCrop(224),
                transforms.ToTensor(),
                normalize
            ])
            self.model_cls = models.resnet50(pretrained=True)
            self.use_GPU = False
            self.expected_params = {
                'model_cls': self.model_cls,
                # 'model_cls': None,
                'model_uri': None,
                'transform': self.transform,
                'resize_val': 256,
                'batch_size': 8,
                'use_gpu': self.use_GPU,
                'gpu_device_id': 0,
            }

        def test_impl_findable(self):
            self.assertIn(PytorchDescriptorGenerator.__name__,
                                 get_descriptor_generator_impls())

        @mock.patch('smqtk.algorithms.descriptor_generator.pytorch_descriptor'
                    '.PytorchDescriptorGenerator._setup_network')
        def test_get_config(self, m_cdg_setupNetwork):
            # Mocking set_network so we don't have to worry about actually
            # initializing any pytorch things for this test.

            # make sure that we're considering all constructor parameter options
            expected_param_keys = \
                set(inspect.getfullargspec(PytorchDescriptorGenerator.__init__)
                           .args[1:])
            self.assertSetEqual(set(self.expected_params.keys()),
                                        expected_param_keys)
            g = PytorchDescriptorGenerator(**self.expected_params)
            self.assertEqual(g.get_config(), self.expected_params)


        @mock.patch('smqtk.algorithms.descriptor_generator.pytorch_descriptor'
                    '.PytorchDescriptorGenerator._setup_network')
        def test_pickle_save_restore(self, m_cdg_setupNetwork):

            g = PytorchDescriptorGenerator(**self.expected_params)
            # Initialization sets up the network on construction.
            self.assertEqual(m_cdg_setupNetwork.call_count, 1)

            g_pickled = pickle.dumps(g, -1)
            g2 = pickle.loads(g_pickled)
            # Network should be setup for second class class just like in
            # initial construction.
            self.assertEqual(m_cdg_setupNetwork.call_count, 2)
            self.assertIsInstance(g2, PytorchDescriptorGenerator)


        def test_copied_descriptorGenerator(self):
            # When use_GPU is True, the  
            if self.use_GPU is False:
                g = PytorchDescriptorGenerator(**self.expected_params)
                g_pickled = pickle.dumps(g, -1)
                g2 = pickle.loads(g_pickled)

                from smqtk.representation.descriptor_element_factory import DescriptorElementFactory
                from smqtk.representation.descriptor_element.local_elements import DescriptorMemoryElement
                lenna_elem = from_uri(self.lenna_image_fp)
                factory = DescriptorElementFactory(DescriptorMemoryElement, {})
                d = g.compute_descriptor(lenna_elem, factory).vector()
                d2 = g2.compute_descriptor(lenna_elem, factory).vector()
                numpy.testing.assert_allclose(d, d2, atol=1e-8)
            else:
                pass


        @mock.patch('smqtk.algorithms.descriptor_generator.pytorch_descriptor'
                    '.PytorchDescriptorGenerator._setup_network')
        def test_invalid_datatype(self, m_cdg_setupNetwork):
            self.assertRaises(
                ValueError,
                PytorchDescriptorGenerator,
                None, None, None
            )


        @mock.patch('smqtk.algorithms.descriptor_generator.caffe_descriptor'
                    '.CaffeDescriptorGenerator._setup_network')
        def test_no_internal_compute_descriptor(self, m_cdg_setupNetwork):
            # This implementation's descriptor computation logic sits in async
            # method override due to caffe's natural multi-element computation
            # interface. Thus, ``_compute_descriptor`` should not be
            # implemented.

            # dummy network setup because _setup_network is mocked out
            g = PytorchDescriptorGenerator(**self.expected_params)
            self.assertRaises(
                NotImplementedError,
                g._compute_descriptor, None
            )


        def test_compare_descriptors(self):
            # Compare the extracted feature is equal to the one
            # extracted directly from the model.
            d = PytorchDescriptorGenerator(**self.expected_params)
            lenna_elem = from_uri(self.lenna_image_fp)
            descr = d.compute_descriptor(lenna_elem).vector()

            from PIL import Image
            from torch.autograd import Variable
            img = Image.open(self.lenna_image_fp)
            img = img.resize((256, 256), Image.BILINEAR).convert('RGB')
            img = self.transform(img)
            if self.use_GPU:
                img = img.cuda()
            expected_descr = self.model_cls(Variable(img.unsqueeze(0)))
            if self.use_GPU:
                expected_descr = expected_descr.data.cpu().squeeze().numpy()
            else:
                expected_descr = expected_descr.data.squeeze().numpy()
            numpy.testing.assert_allclose(descr, expected_descr, atol=1e-8)


        def test_compute_descriptor_async_no_data(self):
            # Should get a ValueError when given no descriptors to async method
            g = PytorchDescriptorGenerator(**self.expected_params)
            self.assertRaises(
                ValueError,
                g.compute_descriptor_async, []
            )
