#!/usr/bin/env python
# -*- coding: utf-8 -*-
# File: nn.py

"""
network architecture settings
backbone: resnet
"""

from self_utils import *

import argparse
import os
import numpy as np

from tensorpack import *
from tensorpack.tfutils.summary import add_moving_summary, add_param_summary
from tensorpack.utils.gpu import get_num_gpu

import tensorflow as tf


BATCH_SIZE = 128
HEIGHT = 32
WIDTH = 32


class Model(ModelDesc):

    def __init__(self, n):
        super(Model, self).__init__()
        self.n = n

    def inputs(self):
        return [tf.placeholder(tf.float32, [None, HEIGHT, WIDTH, 3], 'input_image'),
                tf.placeholder(tf.float32, [None, HEIGTH, WIDTH], 'input_depthmap')]

    def build_graph(self, image, depth_map):
        # b * h * w * c
        image = self._preprocess_image(image)
        # b * h * w
        original_depth_map = self._preprocess_image(depth_map)

        # b, c, h, w
        image = tf.transpose(image, [0, 3, 1, 2])

        # b, c, h, w
        # original_depth_map = tf.expand_dims(original_depth_map, 1)

        depth_map = resize_image(depth_map, H/2, W/2, 'channels_last')
        depth_map = tf.expand_dims(depth_map, 1)
        assert tf.test.is_gpu_available()

        

        # residual block
        # the same as the variant of resnet in "identity mapping in resnet"
        with argscope([Conv2D, AvgPooling, BatchNorm, GlobalAvgPooling], data_format='channels_first'), \
                argscope(Conv2D, use_bias=False, kernel_size=3,
                         kernel_initializer=tf.variance_scaling_initializer(scale=2.0, mode='fan_out')):
            # 64*H/2*W/2
            l = Conv2D('conv0', image, filters=64, strides=2, activation=BNReLU)

            # 64*H/4*W/4
            l = AvgPooling('pool0', l, pool_size=2, strides=2)

            # 256*H/4*W/4

            l = residual('res1.0', l, first=True)
            for k in range(1, self.n):
                l = residual(f"res1.{k}", l)
            
            # 512*H/8*W/8           
            l = residual("res2.0", l, increase_dim=True) # strided residual block
            for k in range(1, self.n):
                l = residual(f"res2.{k}", l)
            
            # 1024*H/16*W/16            
            l = residual("res3.0", l, increase_dim=True) # strided residual block
            for k in range(1, self.n):
                l = residual(f"res3.{k}", l)

            # 2048*H/32*W/32
            l = residual("res4.0", l, increase_dim=True)
            for k in range(1, self.n):
                l = residual(f"res4.{k}", l)

            l = BNReLU('down_bnlast', l)

            """
            up-sampling
            Besides, the up-sampling block adopt the spirit of residual block,
            where we create a direct path for information passing.
            Also, we use the method of preactivation
            """
            
            # 1024*H/32*W/32
            l = Conv2D('conv1', l, filters=1024, activation=BNReLU)
            
            # first up-sampling block
            with tf.variable_scope("up1.0"):
                
                # @WARN: resize_image_with_pad only accept the image with the format of channels last
                # here I use up-sampling rather than unpooling
                # 1024*H/16*W/16
                l = resize_image(l, HEIGHT/16, WIDTH/16)

                # 512*H/16*W/16
                shortcut = Conv2D('conv0', l, filters=512, kernel_size=1)

                c1 = Conv2D('conv1', l, filters=512, activation=BNReLU)
                c2 = Conv2D('conv2', c1, filters=512, activation=BNReLU)
                c3 = Conv2D('conv3', c2, filters=512)

                # 512*H/16*W/16
                # elementwise SUM layer
                l = shortcut + c3                
            
            with tf.variable_scope("up2.0"):
                # pre-act
                l = BNReLU(l)

                # 512*H/8*W/8
                l = resize_image(l, HEIGHT/8, WIDTH/8)
                # the channels does not decrease, just simply pass the information
                shortcut = l

                side_input = resize_image(depth_map, HEIGHT/8, WIDTH/8)
                side_input = Conv2D('stride_conv0', side_input, 64, kernel_size=1, activation=BNReLU)

                concat_ret = tf.concat(values=[l, side_input], axis=1)

                c1 = Conv2D('conv1', concat_ret, 512, activation=BNReLU)
                c2 = Conv2D('conv2', c1, 512, activation=BNReLU)
                c3 = Conv2D('conv3', c2, 512)

                # 512*H/8*W/8
                l = shortcut + c3
            
            with tf.variable_scope('up3.0'):
                # pre-act
                l = BNReLU(l)
                # 512*H/8*W/8
                side_output = Conv2D('multi_conv0', l, 1, kernel_size=1)

                # 512*H/4*W/4
                l = resize_image(l, HEIGHT/4, WIDTH/4)
                shortcut = Conv2D('conv0', l, 256, kernel_size=1)

                side_input = resize_image(depth_map, HEIGHT/4, WIDTH/4)
                side_input = Conv2D('stride_conv0', side_input, 64, kernel_size=1, activation=BNReLU)
                concat_ret = tf.concat(values=[l, side_input], axis=1)

                # 256*H/4*W/4
                c1 = Conv2D('conv1', concat_ret, 256, activation=BNReLU)
                c2 = Conv2D('conv2', c1, 256, activation=BNReLU)
                c3 = Conv2D('conv3', c2, 256)

                # 256*H/4*W/4
                l = shortcut + c3

            with tf.variable_scope('up4.0'):
                # pre-act
                l = BNReLU(l)
                # 256*H/4*W/4
                side_output = Conv2D('multi_conv1', l, 1, kernel_size=1)

                # 256*H/2*W/2
                l = resize_image(l, HEIGHT/2, WIDTH/2)
                shortcut = Conv2D('conv0', l, 128, kernel_size=1)

                # @WARN: actually, the default is H/2*W/2 for depth map
                side_input = resize_image(depth_map, HEIGHT/2, WIDTH/2)
                side_input = Conv2D('stride_conv0', side_input, 64, kernel_size=1, activation=BNReLU)
                concat_ret = tf.concat(values=[l, side_input], axis=1)
                
                # 128*H/2*W/2
                c1 = Conv2D('conv1', concat_ret, 128, activation=BNReLU)
                c2 = Conv2D('conv2', c1, 128, activation=BNReLU)
                c3 = Conv2D('conv3', c2, 128)

                l = shortcut + c3
            
            with tf.variable_scope('output'):
                # pre-act
                l = BNReLU(l)
                # 128*H/2*W/2
                side_output = Conv2D('multi_conv1', l, 1, kernel_size=1)

                # 128*H*W
                l = resize_image(l, HEIGHT, WIDTH)
                
                # 64*H*W
                c1 = Conv2D('conv1', l, 64, activation=BNReLU)
                c2 = Conv2D('conv2', c1, 64, activation=BNReLU)

                # b*1*H*W
                output = Conv2D('output_conv', c2, 1, kernel_size=1, activation=BNReLU)

        batch_size, height, width = output.get_shape().as_list()[:3]
        loss = get_loss(original_depth_map, tf.reshape(output, [batch_size, height, width]), name='loss')

        add_moving_summary(loss)
        add_param_summary(('.*/W', ['histogram']))

        return loss

    def optimizer(self):
        lr = tf.get_variable('learning_rate', initializer=0.01, trainable=False)
        return tf.train.MomentumOptimizer(lr, 0.9)

                                        

            




            


                


                
                
                

                
               
            






        




        


        

    def _preprocess_image():
        pass
        
    

if __name__ == "__main__":
    pass

