# Copyright (c) 2017, Carnegie Mellon University. All rights reserved.
#
# Use of the K-NRM package is subject to the terms of the software license set
# forth in the LICENSE file included with this software, and also available at
# https://github.com/AdeDZY/K-NRM/blob/master/LICENSE
# encoding='utf-8'
import os

import tensorflow as tf
import numpy as np
from knrm.data import DataGenerator
from traitlets.config import Configurable
from traitlets import (
    Int,
    Float,
    Bool,
    Unicode,
)
import sys
import time
import argparse
from traitlets.config.loader import PyFileConfigLoader
from knrm.model import BaseNN


def get_file_line_count(file_path):
    count = 0
    f = open(file_path)
    for line in f:
        count += 1
    f.close()
    return count


class KNRM(BaseNN):
    neg_sample = 1

    lamb = Float(0.5, help="guassian_sigma = lamb * bin_size").tag(config=True)
    emb_in = Unicode('None', help="initial embedding. Terms should be hashed to ids.").tag(config=True)
    learning_rate = Float(0.001, help="learning rate, default is 0.001").tag(config=True)
    epsilon = Float(0.00001, help="Epsilon for Adam").tag(config=True)
    num_filters = Int(128, help='convolution filter number').tag(config=True)
    kernel_sizes = [1, 2, 3]

    def __init__(self, **kwargs):
        super(KNRM, self).__init__(**kwargs)

        self.mus = KNRM.kernal_mus(self.n_bins, use_exact=True)
        self.sigmas = KNRM.kernel_sigmas(self.n_bins, self.lamb, use_exact=True)
        print("kernel sigma values: ", self.sigmas)

        print("trying to load initial embeddings from:  ", self.emb_in)
        if self.emb_in != 'None':
            self.emb = self.load_word2vec(self.emb_in)
            self.embeddings = tf.Variable(
                tf.constant(self.emb, dtype='float32', shape=[self.vocabulary_size + 1, self.embedding_size]))
            print("Initialized embeddings with {0}".format(self.emb_in))
        else:
            self.embeddings = tf.Variable(tf.random_uniform([self.vocabulary_size + 1, self.embedding_size], -1.0, 1.0))

        # Model parameters for feedfoward rank NN
        self.W1 = KNRM.weight_variable([self.n_bins, 1])
        self.b1 = tf.Variable(tf.zeros([1]))
        length = pow(len(self.kernel_sizes), 2)
        self.W2 = KNRM.weight_variable([self.n_bins * length, 1])

    def load_word2vec(self, emb_file_path):
        emb = np.random.uniform(low=-1, high=1, size=(self.vocabulary_size + 1, self.embedding_size))
        nlines = 0
        with open(emb_file_path) as f:
            for line in f:
                nlines += 1
                if nlines == 1:
                    continue
                items = line.split()
                tid = int(items[0])
                if tid > self.vocabulary_size:
                    print(tid)
                    continue
                vec = np.array([float(t) for t in items[1:]])
                emb[tid, :] = vec
                if nlines % 20000 == 0:
                    print("load {0} vectors...".format(nlines))
        return emb

    def model(self, inputs_q, inputs_d, mask, q_weights, mu, sigma):
        """
        The pointwise model graph
        :param inputs_q: input queries. [nbatch, qlen, emb_dim]
        :param inputs_d: input documents. [nbatch, dlen, emb_dim]
        :param mask: a binary mask. [nbatch, qlen, dlen]
        :param q_weights: query term weigths. Set to binary in the paper.
        :param mu: kernel mu values.
        :param sigma: kernel sigma values.
        :return: return the predicted score for each <query, document> in the batch
        """
        # look up embeddings for each term. [nbatch, qlen, emb_dim]
        q_embed = tf.nn.embedding_lookup(self.embeddings, inputs_q, name='qemb')
        d_embed = tf.nn.embedding_lookup(self.embeddings, inputs_d, name='demb')

        ## Uingram Model
        # normalize and compute similarity matrix
        norm_q = tf.sqrt(tf.reduce_sum(tf.square(q_embed), 2, keep_dims=True))
        normalized_q_embed = q_embed / norm_q
        norm_d = tf.sqrt(tf.reduce_sum(tf.square(d_embed), 2, keep_dims=True))
        normalized_d_embed = d_embed / norm_d
        tmp = tf.transpose(normalized_d_embed, perm=[0, 2, 1])

        # similarity matrix [n_batch, qlen, dlen]
        sim = tf.matmul(normalized_q_embed, tmp, name='similarity_matrix')

        # compute gaussian kernel
        rs_sim = tf.reshape(sim, [self.batch_size, self.max_q_len, self.max_d_len, 1])

        # compute Gaussian scores of each kernel
        # batch_size * q_len * d_len * num_bins
        tmp = tf.exp(-tf.square(tf.subtract(rs_sim, mu)) / (tf.multiply(tf.square(sigma), 2)))

        # mask those non-existing words.
        tmp = tmp * mask

        feats = []  # store the soft-TF features from each field.
        # sum up gaussian scores
        # [batch_size, q_len, n_bins]
        kde = tf.reduce_sum(tmp, [2])
        kde = tf.log(tf.maximum(kde, 1e-10)) * 0.01  # 0.01 used to scale down the data.

        # aggregated query terms
        # q_weights = [1, 1, 0, 0...]. Works as a query word mask.
        # Support query-term weighting if set to continous values (e.g. IDF).
        # batch_size * n_bins
        aggregated_kde = tf.reduce_sum(kde * q_weights, [1])  # [batch, n_bins]

        feats.append(aggregated_kde)  # [[batch, nbins]]
        feats_tmp = tf.concat(feats, 1)  # [batch, n_bins]
        print("batch feature shape:", feats_tmp.get_shape())

        # Reshape. (maybe not necessary...)
        feats_flat = tf.reshape(feats_tmp, [-1, self.n_bins])
        print("flat feature shape:", feats_flat.get_shape())

        lo = tf.matmul(feats_flat, self.W1) + self.b1

        # Learning-To-Rank layer. o is the final matching score.
        o = tf.tanh(lo)

        # data parameters
        total_parameters = 0
        for variable in tf.trainable_variables():
            shape = variable.get_shape()
            # print(shape)
            # print(len(shape))
            variable_parametes = 1
            for dim in shape:
                # print(dim)
                variable_parametes *= dim.value
            # print(variable_parametes)
            total_parameters += variable_parametes
        print("total number of parameters:", total_parameters)

        # return some mid result and final matching score.
        return (sim, feats_flat, lo), o

    def conv_model(self, inputs_q, inputs_d, mask, q_weights, mu, sigma):
        """
        The pointwise model graph
        :param inputs_q: input queries. [nbatch, qlen, emb_dim]
        :param inputs_d: input documents. [nbatch, dlen, emb_dim]
        :param mask: a binary mask. [nbatch, qlen, dlen]
        :param q_weights: query term weigths. Set to binary in the paper.
        :param mu: kernel mu values.
        :param sigma: kernel sigma values.
        :return: return the predicted score for each <query, document> in the batch
        """

        print('convolution model')

        # look up embeddings for each term. [nbatch, qlen, emb_dim]
        q_embed = tf.nn.embedding_lookup(self.embeddings, inputs_q, name='qemb')
        d_embed = tf.nn.embedding_lookup(self.embeddings, inputs_d, name='demb')

        ## Uingram Model
        # normalize and compute similarity matrix
        # norm_q = tf.sqrt(tf.reduce_sum(tf.square(q_embed), 2, keepdims=True))
        # normalized_q_embed = q_embed / norm_q
        # norm_d = tf.sqrt(tf.reduce_sum(tf.square(d_embed), 2, keepdims=True))
        # normalized_d_embed = d_embed / norm_d
        # tmp = tf.transpose(normalized_d_embed, perm=[0, 2, 1])

        with tf.variable_scope("cnn",reuse=tf.AUTO_REUSE):
            q_convs = []
            d_convs = []
            for size in self.kernel_sizes:
                # conv_q: batch_size * q_len * num_filters
                conv_q = tf.layers.conv1d(q_embed, self.num_filters, size, padding='same', activation=tf.nn.relu)
                conv_d = tf.layers.conv1d(d_embed, self.num_filters, size, padding='same', activation=tf.nn.relu)
                q_convs.append(conv_q)
                d_convs.append(conv_d)

            simis = []
            normalized_q_convs = []
            for q_conv in q_convs:
                norm_q_conv = tf.sqrt(tf.reduce_sum(tf.square(q_conv), 2, keep_dims=True))
                normalized_q_conv = q_conv / norm_q_conv
                normalized_q_convs.append(normalized_q_conv)
            normalized_d_conv_ts = []
            for d_conv in d_convs:
                norm_d_conv = tf.sqrt(tf.reduce_sum(tf.square(d_conv), 2, keep_dims=True))
                normalized_d_donv = d_conv / norm_d_conv
                normalized_d_conv_t = tf.transpose(normalized_d_donv, perm=[0, 2, 1])
                normalized_d_conv_ts.append(normalized_d_conv_t)
            for normalized_q_conv in normalized_q_convs:
                for normalized_d_conv_t in normalized_d_conv_ts:
                    simi = tf.matmul(normalized_q_conv, normalized_d_conv_t)
                    simis.append(simi)

        with tf.variable_scope("kernel",reuse=tf.AUTO_REUSE):
            feats = []  # store the soft-TF features from each field.
            for sim in simis:
                # compute Gaussian scores of each kernel
                rs_sim = tf.reshape(sim, [self.batch_size, self.max_q_len, self.max_d_len, 1])
                # batch_size * q_len * d_len * n_bins
                tmp = tf.exp(-tf.square(tf.subtract(rs_sim, mu)) / (tf.multiply(tf.square(sigma), 2)))

                # mask those non-existing words.
                tmp = tmp * mask

                # sum up gaussian scores
                # batch_size * q_len * n_bins
                kde = tf.reduce_sum(tmp, [2])
                # kde = tf.log(tf.maximum(kde, 1e-10)) * 0.01  # 0.01 used to scale down the data.
                kde = tf.log1p(kde)

                # aggregated query terms
                # q_weights = [1, 1, 0, 0...]. Works as a query word mask.
                # Support query-term weigting if set to continous values (e.g. IDF).
                # batch * n_bins
                aggregated_kde = tf.reduce_sum(kde * q_weights, [1])

                feats.append(aggregated_kde)  # [[batch, nbins]]

        with tf.variable_scope("fc",reuse=tf.AUTO_REUSE):
            # feats_tmp=tf.stack(feats,1)
            # feats_tmp=tf.reduce_sum(feats_tmp,[1])
            # lo = tf.layers.dense(feats_tmp, 1)
            # # fc = tf.contrib.layers.dropout(fc, 0.1)
            # o = tf.nn.tanh(lo)

            # num_kernel^2 * batch * n_bins -> batch * [num_kernel^2 * n_bins ]
            feats_tmp = tf.concat(feats, 1)
            print("batch feature shape:", feats_tmp.get_shape())
            lo = tf.matmul(feats_tmp, self.W2) + self.b1
            # Learning-To-Rank layer. o is the final matching score.
            o = tf.tanh(lo)

        # data parameters
        total_parameters = 0
        for variable in tf.trainable_variables():
            shape = variable.get_shape()
            # print(shape)
            # print(len(shape))
            variable_parametes = 1
            for dim in shape:
                # print(dim)
                variable_parametes *= dim.value
            # print(variable_parametes)
            total_parameters += variable_parametes
        print("total number of parameters:", total_parameters)

        # return some mid result and final matching score.
        return (simis, feats_tmp, lo), o

    def train(self, train_pair_file_path, val_pair_file_path, checkpoint_dir, load_model=False, conv=False):

        # PLACEHOLDERS
        # This is where training samples and labels are fed to the graph.
        # These placeholder nodes will be fed a batch of training data at each
        # training step using the {feed_dict} argument to the Run() call below.

        train_size = get_file_line_count(train_pair_file_path)
        print('train sample number: {}'.format(train_size))

        # nodes to hold mu sigma
        input_mu = tf.placeholder(tf.float32, shape=[self.n_bins], name='input_mu')
        input_sigma = tf.placeholder(tf.float32, shape=[self.n_bins], name='input_sigma')

        # nodes to hold query and qterm idf. padding terms will have idf=0
        train_inputs_q = tf.placeholder(tf.int32, shape=[self.batch_size, self.max_q_len], name='train_inputs_q')
        train_input_q_weights = tf.placeholder(tf.float32, shape=[self.batch_size, self.max_q_len], name='idf')

        # nodes to hold training data, postive samples
        train_inputs_pos_d = tf.placeholder(tf.int32, shape=[self.batch_size, self.max_d_len],
                                            name='train_inputs_pos_d')

        # nodes to hold negative samples
        train_inputs_neg_d = tf.placeholder(tf.int32, shape=[self.batch_size, self.max_d_len])

        # mask padding terms
        # assume all termid >= 1
        # padding with 0
        input_train_mask_pos = tf.placeholder(tf.float32, shape=[self.batch_size, self.max_q_len, self.max_d_len])
        input_train_mask_neg = tf.placeholder(tf.float32, shape=[self.batch_size, self.max_q_len, self.max_d_len])

        # reshape place holders
        mu = tf.reshape(input_mu, shape=[1, 1, self.n_bins])
        sigma = tf.reshape(input_sigma, shape=[1, 1, self.n_bins])
        rs_train_mask_pos = tf.reshape(input_train_mask_pos, [self.batch_size, self.max_q_len, self.max_d_len, 1])
        rs_train_mask_neg = tf.reshape(input_train_mask_neg, [self.batch_size, self.max_q_len, self.max_d_len, 1])
        rs_q_weights = tf.reshape(train_input_q_weights, shape=[self.batch_size, self.max_q_len, 1])

        # training graph
        if not conv:
            mid_res_pos, o_pos = self.model(train_inputs_q, train_inputs_pos_d, rs_train_mask_pos, rs_q_weights, mu,
                                            sigma)
            mid_res_neg, o_neg = self.model(train_inputs_q, train_inputs_neg_d, rs_train_mask_neg, rs_q_weights, mu,
                                            sigma)
        else:
            with tf.variable_scope("model") as scope:
                mid_res_pos, o_pos = self.conv_model(train_inputs_q, train_inputs_pos_d, rs_train_mask_pos, rs_q_weights,
                                                     mu, sigma)
                scope.reuse_variables()
                mid_res_neg, o_neg = self.conv_model(train_inputs_q, train_inputs_neg_d, rs_train_mask_neg, rs_q_weights,
                                                 mu, sigma)
        loss = tf.reduce_mean(tf.maximum(0.0, 1 - o_pos + o_neg))

        # optimizer
        optimizer = tf.train.AdamOptimizer(learning_rate=self.learning_rate, epsilon=self.epsilon).minimize(loss)

        # Create a local session to run the training.

        with tf.Session() as sess:

            saver = tf.train.Saver()
            start_time = time.time()

            # Run all the initializers to prepare the trainable parameters.
            if not load_model:
                print("Initializing a new model...")
                tf.initialize_all_variables().run()
                print('New model initialized!')

            else:
                ckpt = tf.train.get_checkpoint_state(checkpoint_dir)
                if ckpt and ckpt.model_checkpoint_path:
                    saver.restore(sess, ckpt.model_checkpoint_path)
                    print("model loaded!")
                else:
                    print("no data found")
                    exit(-1)

            # Loop through training steps.
            step = 0
            smallest_val_loss = 100000
            train_loss = 0
            for epoch in range(int(self.max_epochs)):
                pair_stream = open(train_pair_file_path)
                for BATCH in self.data_generator.pairwise_reader(pair_stream, self.batch_size, with_idf=True):
                    # print('batch size:',self.batch_size)
                    step += 1
                    # print('step: {}'.format(step))
                    X, Y = BATCH
                    M_pos = self.gen_mask(X[u'q'], X[u'd'])
                    M_neg = self.gen_mask(X[u'q'], X[u'd_aux'])

                    if X[u'idf'].shape[0] != self.batch_size:
                        continue
                    train_feed_dict = {train_inputs_q: self.re_pad(X[u'q'], self.batch_size),
                                       train_inputs_pos_d: self.re_pad(X[u'd'], self.batch_size),
                                       train_inputs_neg_d: self.re_pad(X[u'd_aux'], self.batch_size),
                                       train_input_q_weights: self.re_pad(X[u'idf'], self.batch_size),
                                       input_mu: self.mus,
                                       input_sigma: self.sigmas,
                                       input_train_mask_pos: M_pos,
                                       input_train_mask_neg: M_neg}

                    # Run the graph and fetch some of the nodes.
                    _, train_batch_loss = sess.run([optimizer, loss], feed_dict=train_feed_dict)
                    train_loss += train_batch_loss
                    # o_pos_ = sess.run(o_pos, feed_dict=train_feed_dict)
                    # o_neg_ = sess.run(o_neg, feed_dict=train_feed_dict)
                    # print('pos')
                    # print(o_pos_)
                    # print('neg:')
                    # print(o_neg_)
                    #
                    # mid_res_pos_ = sess.run(mid_res_pos, feed_dict=train_feed_dict)
                    # mid_res_neg_ = sess.run(mid_res_neg, feed_dict=train_feed_dict)
                    # print('pos1:')
                    # print(mid_res_pos_[2])
                    # print('neg1:')
                    # print(mid_res_neg_[2])

                    n_val_batch = 0
                    if (step + 1) % self.eval_frequency == 0:

                        # train progress
                        elapsed_time = time.time() - start_time
                        start_time = time.time()
                        print('Step %d (epoch %.2f), %.1f ms per step' % (step,
                                                                          float(step) * self.batch_size / (
                                                                                  train_size * self.neg_sample),
                                                                          1000 * elapsed_time / self.eval_frequency))

                        val_loss = 0
                        val_pair_stream = open(val_pair_file_path)
                        for BATCH in self.val_data_generator.pairwise_reader(val_pair_stream, self.batch_size,
                                                                             with_idf=True):
                            X_val, Y_val = BATCH
                            M_pos = self.gen_mask(X_val[u'q'], X_val[u'd'])
                            M_neg = self.gen_mask(X_val[u'q'], X_val[u'd_aux'])
                            val_feed_dict = {train_inputs_q: self.re_pad(X_val[u'q'], self.batch_size),
                                             train_inputs_pos_d: self.re_pad(X_val[u'd'], self.batch_size),
                                             train_inputs_neg_d: self.re_pad(X_val[u'd_aux'], self.batch_size),
                                             train_input_q_weights: self.re_pad(X_val[u'idf'], self.batch_size),
                                             input_mu: self.mus,
                                             input_sigma: self.sigmas,
                                             input_train_mask_pos: M_pos,
                                             input_train_mask_neg: M_neg}
                            l = sess.run(loss, feed_dict=val_feed_dict)
                            val_loss += l
                            n_val_batch += 1
                        val_pair_stream.close()
                        val_loss /= n_val_batch

                        # output evaluations
                        elapsed_time = time.time() - start_time
                        start_time = time.time()
                        print('Eval elapsed: %.1f ms' % (1000 * elapsed_time))

                        print('train loss: {:.3f}, validation loss: {:.3f}'.format(train_loss / self.eval_frequency,
                                                                                   val_loss))
                        train_loss = 0

                        sys.stdout.flush()

                        if val_loss < smallest_val_loss:
                            smallest_val_loss = val_loss
                            # save model with smallest validation loss
                            saver.save(sess, checkpoint_dir + '/data.ckpt')

                # END epoch
                pair_stream.close()

            saver.save(sess, checkpoint_dir + '/final_data.ckpt')

    def test(self, test_point_file_path, output_file_path, checkpoint_dir=None, load_model=False, conv=False):

        # PLACEHOLDERS
        # This is where training samples and labels are fed to the graph.
        # These placeholder nodes will be fed a batch of training data at each
        # training step using the {feed_dict} argument to the Run() call below.

        test_size = get_file_line_count(test_point_file_path)

        # nodes to hold mu and sigma
        input_mu = tf.placeholder(tf.float32, shape=[self.n_bins], name='input_mu')
        input_sigma = tf.placeholder(tf.float32, shape=[self.n_bins], name='input_sigma')

        # nodes to hold query and qterm idf. padding terms will have idf=0
        test_inputs_q = tf.placeholder(tf.int32, shape=[self.batch_size, self.max_q_len], name='test_inputs_q')
        test_input_q_weights = tf.placeholder(tf.float32, shape=[self.batch_size, self.max_q_len], name='idf')

        # nodes to hold test data
        test_inputs_d = tf.placeholder(tf.int32, shape=[self.batch_size, self.max_d_len],
                                       name='test_inputs_pos_d')

        # mask padding terms
        # assume all docid >= 1
        # assume padded with 0
        test_mask = tf.placeholder(tf.float32, shape=[self.batch_size, self.max_q_len, self.max_d_len])

        # reshape place holders
        mu = tf.reshape(input_mu, shape=[1, 1, self.n_bins])
        sigma = tf.reshape(input_sigma, shape=[1, 1, self.n_bins])
        rs_test_mask = tf.reshape(test_mask, [self.batch_size, self.max_q_len, self.max_d_len, 1])
        rs_q_weights = tf.reshape(test_input_q_weights, shape=[self.batch_size, self.max_q_len, 1])

        # training graph
        if not conv:
            inter_res, o = self.model(test_inputs_q, test_inputs_d, rs_test_mask, rs_q_weights, mu, sigma)
        else:
            with tf.variable_scope("model") as scope:
                inter_res, o = self.conv_model(test_inputs_q, test_inputs_d, rs_test_mask, rs_q_weights, mu, sigma)

        # Create a local session to run the testing.
        with tf.Session() as sess:
            test_point_stream = open(test_point_file_path)
            outfile = open(output_file_path, 'w')
            saver = tf.train.Saver()

            if load_model:
                p = checkpoint_dir + 'data.ckpt'
                saver.restore(sess, p)
                print("data loaded!")
            else:
                tf.initialize_all_variables().run()

            # Loop through training steps.
            for b in range(int(np.ceil(float(test_size) / self.batch_size))):
                X, Y = next(
                    self.test_data_generator.pointwise_generate(test_point_stream, self.batch_size, with_idf=True,
                                                                with_label=False))
                M = self.gen_mask(X[u'q'], X[u'd'])
                test_feed_dict = {test_inputs_q: self.re_pad(X[u'q'], self.batch_size),
                                  test_inputs_d: self.re_pad(X[u'd'], self.batch_size),
                                  test_input_q_weights: self.re_pad(X[u'idf'], self.batch_size),
                                  input_mu: self.mus,
                                  input_sigma: self.sigmas,
                                  test_mask: M}

                # Run the graph and fetch some of the nodes.
                scores = sess.run(o, feed_dict=test_feed_dict)

                for score in scores:
                    outfile.write('{0}\n'.format(score[0]))

            outfile.close()
            test_point_stream.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("config_file_path")

    parser.add_argument("--train", action='store_true')
    parser.add_argument("--train_file", '-f', help="train_pair_file_path")
    parser.add_argument("--validation_file", '-v', help="val_pair_file_path")
    parser.add_argument("--load_model", '-l', action='store_true')

    parser.add_argument("--test", action="store_true")
    parser.add_argument("--test_file")
    parser.add_argument("--output_score_file", '-o')
    parser.add_argument("--emb_file_path", '-e')
    parser.add_argument("--checkpoint_dir", '-s', help="store data to here")

    parser.add_argument("--convolution", '-c', default=False)

    args = parser.parse_args()

    conf = PyFileConfigLoader(args.config_file_path).load_config()

    if args.train:
        nn = KNRM(config=conf)
        nn.train(train_pair_file_path=args.train_file,
                 val_pair_file_path=args.validation_file,
                 checkpoint_dir=args.checkpoint_dir,
                 load_model=args.load_model,
                 conv=args.convolution)
    else:
        nn = KNRM(config=conf)
        nn.test(test_point_file_path=args.test_file,
                output_file_path=args.output_score_file,
                load_model=True,
                checkpoint_dir=args.checkpoint_dir,
                conv=args.convolution)
