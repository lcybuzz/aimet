# /usr/bin/env python3.5
# -*- mode: python -*-
# =============================================================================
#  @@-COPYRIGHT-START-@@
#
#  Copyright (c) 2021, Qualcomm Innovation Center, Inc. All rights reserved.
#
#  Redistribution and use in source and binary forms, with or without
#  modification, are permitted provided that the following conditions are met:
#
#  1. Redistributions of source code must retain the above copyright notice,
#     this list of conditions and the following disclaimer.
#
#  2. Redistributions in binary form must reproduce the above copyright notice,
#     this list of conditions and the following disclaimer in the documentation
#     and/or other materials provided with the distribution.
#
#  3. Neither the name of the copyright holder nor the names of its contributors
#     may be used to endorse or promote products derived from this software
#     without specific prior written permission.
#
#  THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
#  AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
#  IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
#  ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
#  LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
#  CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
#  SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
#  INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
#  CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
#  ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
#  POSSIBILITY OF SUCH DAMAGE.
#
#  SPDX-License-Identifier: BSD-3-Clause
#
#  @@-COPYRIGHT-END-@@
# =============================================================================
"""Cross Layer Equalization"""

import collections
import typing

import numpy as np
import tensorflow as tf
import libpymo

from aimet_common.utils import AimetLogger
from aimet_tensorflow.keras.utils.weight_tensor_utils import WeightTensorUtils

_logger = AimetLogger.get_area_logger(AimetLogger.LogAreas.CrosslayerEqualization)

ClsSet = typing.Union[typing.Tuple[tf.keras.layers.Conv2D,
                                   tf.keras.layers.Conv2D],
                      typing.Tuple[tf.keras.layers.Conv2D,
                                   tf.keras.layers.DepthwiseConv2D,
                                   tf.keras.layers.Conv2D]]


class ClsSetInfo:
    """
    This class hold information about the layers in a CLS set, along with corresponding scaling factors
    and other information like if there is a ReLU activation function between the CLS set layers
    """

    class ClsSetLayerPairInfo:
        """
        Models a pair of layers that were scaled using CLS. And related information.
        """

        def __init__(self, layer1: tf.keras.layers.Conv2D, layer2: tf.keras.layers.Conv2D, scale_factor: np.ndarray,
                     relu_activation_between_layers: bool):
            """
            :param layer1: Layer whose bias is folded
            :param layer2: Layer to which bias of previous layer's bias is folded
            :param scale_factor: Scale Factor found from Cross Layer Scaling to scale BN parameters
            :param relu_activation_between_layers: If the activation between layer1 and layer2 is Relu
            """
            self.layer1 = layer1
            self.layer2 = layer2
            self.scale_factor = scale_factor
            self.relu_activation_between_layers = relu_activation_between_layers

    def __init__(self, cls_pair_1: ClsSetLayerPairInfo, cls_pair_2: ClsSetLayerPairInfo = None):
        """
        Constructor takes 2 pairs if Depth-wise separable layer is being folded

        :param cls_pair_1: Pair between two conv or conv and depth-wise conv
        :param cls_pair_2: Pair between depth-wise conv and point-wise conv
        """
        if cls_pair_2:
            self.cls_pair_info_list = [cls_pair_1, cls_pair_2]
        else:
            self.cls_pair_info_list = [cls_pair_1]


class GraphSearchUtils:
    """Implements graph search utils required by CLE feature"""

    def __init__(self):
        pass

    @staticmethod
    def convert_layer_group_to_cls_sets(layer_group: typing.List[tf.keras.layers.Conv2D]) \
            -> typing.List[ClsSet]:
        """
        Helper function to convert a layer group to a list of cls sets
        :param layer_group: Given layer group to convert
        :return: List of cls sets
        """
        cls_sets = []

        layer_group = collections.deque(layer_group)
        prev_layer_to_scale = layer_group.popleft()
        while layer_group:
            next_layer_to_scale = layer_group.popleft()

            if isinstance(next_layer_to_scale, tf.keras.layers.DepthwiseConv2D):
                next_non_depthwise_conv_layer = layer_group.popleft()
                # DepthwiseConv layer right after DepthwiseConv layer is not currently supported
                if isinstance(next_non_depthwise_conv_layer, tf.keras.layers.DepthwiseConv2D):
                    _logger.error("Consecutive DepthwiseConv layer not currently supported")
                    raise NotImplementedError

                cls_sets.append(
                    (prev_layer_to_scale, next_layer_to_scale, next_non_depthwise_conv_layer))
                prev_layer_to_scale = next_non_depthwise_conv_layer
            else:
                cls_sets.append((prev_layer_to_scale, next_layer_to_scale))
                prev_layer_to_scale = next_layer_to_scale

        return cls_sets

    @staticmethod
    def is_relu_activation_present_in_cls_sets(cls_sets: typing.List[ClsSet]) \
            -> typing.List[typing.Union[bool, typing.Tuple[bool, bool]]]:
        """
        Check if there is ReLU or PReLU activation between cls sets
        :param cls_sets: List of ClsSet to find ReLU activation in
        :return: List of ReLU activation preset flags (bool or tuple of bool) corresponding to input cls_sets param
        """

        is_relu_activation_in_cls_sets = []
        for cls_set in cls_sets:
            cls_set = cls_set[:-1]

            is_relu_activation_in_cls_set = []
            for layer in cls_set:
                has_relu_activation = GraphSearchUtils._does_layer_have_relu_activation(layer)
                is_relu_activation_in_cls_set.append(has_relu_activation)

            if len(is_relu_activation_in_cls_set) == 1:
                is_relu_activation_in_cls_sets.append(is_relu_activation_in_cls_set[0])
            else:
                is_relu_activation_in_cls_sets.append(tuple(is_relu_activation_in_cls_set))

        return is_relu_activation_in_cls_sets

    @staticmethod
    def _does_layer_have_relu_activation(layer: tf.keras.layers.Conv2D) -> bool:
        """
        Check if layer has ReLU or PReLU activation function
        :param layer: Conv2D or it's subclass to check activation function
        :return: True If layer has ReLU or PReLU activation, otherwise False
        """
        activation_info = tf.keras.activations.serialize(layer.activation)

        if isinstance(activation_info, str):
            # Instantiating like tf.keras.layers.Conv2D(8, kernel_size=3, activation=tf.keras.activations.relu)
            #   has the result of serialization as str type
            activation_type = activation_info
        elif isinstance(activation_info, dict):
            # Instantiating like tf.keras.layers.Conv2D(8, kernel_size=3, activation=tf.keras.layers.ReLU())
            #   has the result of serialization as dict type
            activation_type = activation_info["class_name"].lower()
        else:
            raise NotImplementedError("Not supported format")

        # If activation parameter is not set or None, default activation_type is linear
        if activation_type == "linear" and layer.outbound_nodes:
            assert len(layer.outbound_nodes) == 1

            outbound_layer = layer.outbound_nodes[0].outbound_layer
            return isinstance(outbound_layer, (tf.keras.layers.ReLU, tf.keras.layers.PReLU))

        return activation_type in ["relu", "prelu"]


class CrossLayerScaling:
    """
    Code to apply the cross-layer-scaling technique to a model
    """

    @staticmethod
    def scale_cls_set_with_conv_layers(
            cls_set: typing.Tuple[tf.keras.layers.Conv2D, tf.keras.layers.Conv2D]) -> np.ndarray:
        """
        API to invoke equalize layer params (update for weights and bias is in place)
        :param cls_set: Consecutive Conv layers Tuple whose weights and biases need to be equalized
        :return: Scaling factor S_12 for each conv layer pair: numpy array
        """

        for layer in cls_set:
            # NOTE: DepthwiseConv2D and Conv2DTranspose is subclass of Conv2D
            #   The check below covers all of Conv2D, DepthwiseConv2D and Conv2DTranspose class
            if not isinstance(layer, tf.keras.layers.Conv2D):
                raise ValueError("Only Conv or Transposed Conv layers are supported for CLE")

        scaling_factor, prev_layer_params, curr_layer_params = CrossLayerScaling.call_mo_scale(cls_set)

        prev_layer, curr_layer = cls_set
        weight_and_bias_0 = CrossLayerScaling._unpack_equalization_params(prev_layer, prev_layer_params,
                                                                          unpack_bias=True)
        prev_layer.set_weights(weight_and_bias_0)

        weight_and_bias_1 = CrossLayerScaling._unpack_equalization_params(curr_layer, curr_layer_params,
                                                                          unpack_bias=False)
        curr_layer.set_weights(weight_and_bias_1)

        return scaling_factor

    @staticmethod
    def call_mo_scale(cls_set: typing.Tuple[tf.keras.layers.Conv2D, tf.keras.layers.Conv2D]) \
            -> typing.Tuple[np.ndarray, libpymo.EqualizationParams, libpymo.EqualizationParams]:
        """
        Invokes scale API in model optimization library
        :param cls_set: Consecutive Conv layers Tuple whose weights and biases need to be equalized
        :return: Scaling factor, prev and current layer updated parameters
        """
        prev_layer_params = CrossLayerScaling._pack_equalization_params(cls_set[0], pack_bias=True)
        curr_layer_params = CrossLayerScaling._pack_equalization_params(cls_set[1], pack_bias=False)

        scaling_factor = libpymo.scaleLayerParams(prev_layer_params, curr_layer_params)
        return scaling_factor, prev_layer_params, curr_layer_params

    @staticmethod
    def scale_cls_set_with_depthwise_conv_layers(
            cls_set: typing.Tuple[tf.keras.layers.Conv2D,
                                  tf.keras.layers.DepthwiseConv2D,
                                  tf.keras.layers.Conv2D]) -> typing.Tuple[np.ndarray, np.ndarray]:
        """
        API to invoke equalize layer params (update for weights and bias is in place)
        :param cls_set: Consecutive Conv layers whose weights and biases need to be equalized.
                        Second Conv layer is a depth-wise conv and third conv layer is point-wise conv
        :return: Scaling factors S_12 and S_23 : numpy arrays
        """

        for layer in cls_set:
            # NOTE: DepthwiseConv2D and Conv2DTranspose is subclass of Conv2D
            #   The check below covers all of Conv2D, DepthwiseConv2D and Conv2DTranspose class
            if not isinstance(layer, tf.keras.layers.Conv2D):
                raise ValueError("Only Conv or Transposed Conv layers are supported for CLE")

        scaling_params, prev_layer_params, curr_layer_params, next_layer_params = \
            CrossLayerScaling.call_mo_scale_depthwise_separable_layer(cls_set)

        prev_layer, curr_layer, next_layer = cls_set
        weight_and_bias_0 = CrossLayerScaling._unpack_equalization_params(prev_layer,
                                                                          prev_layer_params,
                                                                          unpack_bias=True)
        prev_layer.set_weights(weight_and_bias_0)

        weight_and_bias_1 = CrossLayerScaling._unpack_equalization_params(curr_layer,
                                                                          curr_layer_params,
                                                                          unpack_bias=True)
        curr_layer.set_weights(weight_and_bias_1)

        weight_and_bias_2 = CrossLayerScaling._unpack_equalization_params(next_layer,
                                                                          next_layer_params,
                                                                          unpack_bias=False)
        next_layer.set_weights(weight_and_bias_2)

        return scaling_params.scalingMatrix12, scaling_params.scalingMatrix23

    @staticmethod
    def call_mo_scale_depthwise_separable_layer(
            cls_set: typing.Tuple[tf.keras.layers.Conv2D,
                                  tf.keras.layers.DepthwiseConv2D,
                                  tf.keras.layers.Conv2D]) -> typing.Tuple[libpymo.RescalingParamsVectors,
                                                                           libpymo.EqualizationParams,
                                                                           libpymo.EqualizationParams,
                                                                           libpymo.EqualizationParams]:
        """
        Invokes scale API in model optimization library
        :param cls_set: Consecutive Conv layers whose weights and biases need to be equalized
        :return: Scaling factors, prev, current and next layer updated parameters
        """

        prev_layer_params = CrossLayerScaling._pack_equalization_params(cls_set[0], pack_bias=True)
        curr_layer_params = CrossLayerScaling._pack_equalization_params(cls_set[1], pack_bias=True)
        next_layer_params = CrossLayerScaling._pack_equalization_params(cls_set[2], pack_bias=False)

        scaling_params = libpymo.scaleDepthWiseSeparableLayer(prev_layer_params, curr_layer_params, next_layer_params)
        return scaling_params, prev_layer_params, curr_layer_params, next_layer_params

    @staticmethod
    def _pack_equalization_params(layer: tf.keras.layers.Conv2D, pack_bias: bool) -> libpymo.EqualizationParams:
        equalization_params = libpymo.EqualizationParams()

        param_tensors = layer.get_weights()

        weight_tensor = param_tensors[0]
        weight_tensor = WeightTensorUtils.transpose_from_tf_to_libpymo_format(weight_tensor, layer)

        equalization_params.weight = weight_tensor.reshape(-1)
        equalization_params.weightShape = np.array(weight_tensor.shape)

        if pack_bias:
            if layer.use_bias:
                equalization_params.bias = param_tensors[1]
            else:
                equalization_params.isBiasNone = True

        return equalization_params

    @staticmethod
    def _unpack_equalization_params(layer: tf.keras.layers.Conv2D,
                                    equalization_params: libpymo.EqualizationParams,
                                    unpack_bias: bool) -> typing.List:

        weight_tensor = np.reshape(equalization_params.weight, equalization_params.weightShape)
        weight_tensor = WeightTensorUtils.transpose_from_libpymo_to_tf_format(weight_tensor, layer)

        if layer.use_bias:
            if unpack_bias:
                bias_tensor = np.reshape(equalization_params.bias, equalization_params.weightShape[0])
            else:
                _, bias_tensor = layer.get_weights()

            param_tensors = [weight_tensor, bias_tensor]
        else:
            param_tensors = [weight_tensor]

        return param_tensors


class HighBiasFold:
    """
    Code to apply the high-bias-fold technique to a model
    """

    @staticmethod
    def bias_fold(cls_set_info_list: typing.List[ClsSetInfo],
                  bn_layers: typing.Dict[tf.keras.layers.Conv2D, tf.keras.layers.BatchNormalization]):
        """
        Folds bias values greater than 3 * sigma to next layer's bias

        :param cls_set_info_list: List of info elements for each cls set
        :param bn_layers: Key: Conv/Linear layer Value: Corresponding folded BN layer
        """
        if not bn_layers:
            _logger.info('High Bias folding is not supported for models without BatchNorm Layers')
            return

        for cls_set_info in cls_set_info_list:
            for cls_pair_info in cls_set_info.cls_pair_info_list:
                if (not cls_pair_info.layer1.use_bias) or (not cls_pair_info.layer2.use_bias) or \
                        (cls_pair_info.layer1 not in bn_layers):
                    continue

                prev_layer_params, curr_layer_params = HighBiasFold.call_mo_high_bias_fold(cls_pair_info, bn_layers)

                layer1 = cls_pair_info.layer1
                layer1_weight_tensor, _ = layer1.get_weights()
                layer1_bias_tensor = np.array(prev_layer_params.bias)
                layer1.set_weights([layer1_weight_tensor, layer1_bias_tensor])

                layer2 = cls_pair_info.layer2
                layer2_weight_tensor, _ = layer2.get_weights()
                layer2_bias_tensor = np.array(curr_layer_params.bias)
                layer2.set_weights([layer2_weight_tensor, layer2_bias_tensor])

    @staticmethod
    def call_mo_high_bias_fold(cls_pair_info: ClsSetInfo.ClsSetLayerPairInfo,
                               bn_layers: typing.Dict[tf.keras.layers.Conv2D, tf.keras.layers.BatchNormalization]) \
            -> typing.Tuple[libpymo.LayerParams, libpymo.LayerParams]:
        """
        Invokes high bias fold MO API

        :param cls_pair_info: Pair of layers that were scaled using CLS and related information
        :param bn_layers: Key: Conv/Linear layer Value: Corresponding folded BN layer
        :return: Updated layer params
        """

        bn_layer = bn_layers[cls_pair_info.layer1]
        prev_layer_bn_params = HighBiasFold._pack_bn_params_high_bias_fold(bn_layer, cls_pair_info.scale_factor)

        prev_layer_params, curr_layer_params = HighBiasFold._pack_layer_params(cls_pair_info)

        libpymo.updateBias(prev_layer_params, curr_layer_params, prev_layer_bn_params)
        return prev_layer_params, curr_layer_params

    @staticmethod
    def _pack_bn_params_high_bias_fold(bn_layer: tf.keras.layers.BatchNormalization,
                                       scaling_parameter: np.ndarray) -> libpymo.BNParamsHighBiasFold:
        """
        Helper method to pack BatchNormalization parameter for high bias fold

        :param bn_layer: Target batch normalization layer
        :param scaling_parameter: Scaling parameters for each channel obtained from cross layer scaling
        :return: Packed BNParamsHighBiasFold
        """
        bn_params = libpymo.BNParamsHighBiasFold()

        gamma, beta, _, _ = bn_layer.get_weights()

        if len(scaling_parameter) != len(gamma) or len(scaling_parameter) != len(beta):
            raise ValueError("High Bias absorption is not supported for networks with fold-forward BatchNorms")

        bn_params.gamma = np.divide(gamma, scaling_parameter)
        bn_params.beta = np.divide(beta, scaling_parameter)

        return bn_params

    @staticmethod
    def _pack_layer_params(cls_pair_info: ClsSetInfo.ClsSetLayerPairInfo) \
            -> typing.Tuple[libpymo.LayerParams, libpymo.LayerParams]:
        """
        Helper method to pack information of previous and current layer

        :param cls_pair_info: Pair of layers that were scaled using CLS and related information
        :return: Packed layer parameter tuple
        """
        # Pack parameters for previous layer
        prev_layer_params = libpymo.LayerParams()

        prev_layer = cls_pair_info.layer1
        prev_layer_params.activationIsRelu = cls_pair_info.relu_activation_between_layers

        _, prev_layer_bias_tensor = prev_layer.get_weights()
        prev_layer_params.bias = prev_layer_bias_tensor

        # Pack parameters for current layer
        curr_layer_params = libpymo.LayerParams()

        curr_layer = cls_pair_info.layer2
        curr_layer_weight_tensor, curr_layer_bias_tensor = curr_layer.get_weights()
        curr_layer_weight_tensor = WeightTensorUtils.transpose_from_tf_to_libpymo_format(curr_layer_weight_tensor,
                                                                                         curr_layer)

        curr_layer_params.bias = curr_layer_bias_tensor
        curr_layer_params.weight = curr_layer_weight_tensor.reshape(-1)
        curr_layer_params.weightShape = np.array(curr_layer_weight_tensor.shape)

        return prev_layer_params, curr_layer_params
