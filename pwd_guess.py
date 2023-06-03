# -*- coding: utf-8 -*-
# author: William Melicher
from __future__ import print_function

import argparse
import bisect
import cProfile
import collections
import csv
import gzip
import itertools
import json
import logging
import math
import os
import os.path
import random
import re
import string
import subprocess as subp
import sys
import tempfile
import time


# This is a hack to support multiple versions of the keras library.
# It would be better to use a solution like virtualenv.
if 'KERAS_PATH' in os.environ:
    sys.path.insert(0, os.environ['KERAS_PATH'])
import keras
try:
    sys.stderr.write('Using keras version %s\n' % (keras.__version__))
except AttributeError:
    pass

from keras.models import Sequential, model_from_json
from keras.layers.core import Activation, Dense, Dropout
from keras.layers import TimeDistributed, Flatten, Conv1D, recurrent, Embedding
import keras.utils
from keras.callbacks import TensorBoard

from sklearn.utils import shuffle
import numpy as np
import tensorflow as tf
from enum import IntEnum

import pylru


import generator

PASSWORD_END = '\n'
PASSWORD_START = '\t'
SYMBOLS = '~!@#$%^&*(),.<>/?\'"{}[]\\|-_=+;: `'
FNAME_PREFIX_SUBPROCESS_CONFIG = 'child_process.'
FNAME_PREFIX_PROCESS_LOG = 'log.child_process.'
FNAME_PREFIX_PROCESS_OUT = 'out.child_process.'

MEMORY_ONLY = ':memory:'

class Sequence(IntEnum):
    MANY_TO_ONE = 0
    many_to_one = 0
    MANY_TO_MANY = 1
    many_to_many = 1

# From: https://docs.python.org/3.4/library/itertools.html
def grouper(iterable, num, fillvalue=None):
    "Collect data into fixed-length chunks or blocks"
    # grouper('ABCDEFG', 3, 'x') --> ABC DEF Gxx"
    args = [iter(iterable)] * num
    return map(lambda x: filter(lambda y: y, x),
               # pylint: disable=E1101
               itertools.zip_longest(*args, fillvalue=fillvalue))


class CharacterTable():
    def __init__(self, chars, maxlen, embedding=False, padding_character=False,
                 sequence_model=Sequence.MANY_TO_ONE):
        self.chars = sorted(set(chars))
        self.char_indices = dict((c, i) for i, c in enumerate(self.chars))
        self.indices_char = dict((i, c) for i, c in enumerate(self.chars))
        self.maxlen = maxlen
        self.vocab_size = len(self.chars)
        self.char_list = self.chars
        self.padding_character = padding_character
        self.embedding = embedding
        self.sequence_model = sequence_model

    def pad_to_len(self, astring, maxlen=None):
        maxlen = maxlen if maxlen else self.maxlen
        if len(astring) > maxlen:
            return astring[len(astring) - maxlen:]
        if self.padding_character:
            return astring + (PASSWORD_END * (maxlen - len(astring)))
        return astring

    def encode_many(self, string_list, maxlen=None, y_vec=False):
        maxlen = maxlen if maxlen else self.maxlen
        x_str_list = map(lambda x: self.pad_to_len(x, maxlen), string_list)
        if self.embedding and not y_vec:
            x_vec = np.zeros(shape=(len(string_list), maxlen), dtype=np.int8)
        else:
            x_vec = np.zeros((len(string_list), maxlen, self.vocab_size),
                         dtype=np.bool)
        for i, xstr in enumerate(x_str_list):
            self.encode_into(x_vec[i], xstr)
        return x_vec

    def encode_many_chunks(self, string_list, max_input_str_len, maxlen=None, y_vec=False):
        maxlen = maxlen if maxlen else self.maxlen
        chunks_str_list = []
        iters = list(range(maxlen, max_input_str_len, maxlen // 2))
        iters.append(max_input_str_len)
        for a_string in string_list:
            prev_iter = 0
            for i in iters:
                if prev_iter >= len(a_string) and (len(a_string) != 0) or\
                        (len(a_string) == 0 and prev_iter != 0):
                    break
                chunk = a_string[i-maxlen:i]
                chunks_str_list.append(chunk)
                prev_iter = i

        return self.encode_many(chunks_str_list, maxlen, y_vec=y_vec), chunks_str_list

    def y_encode_into(self, Y, C):
        for i, c in enumerate(C):
            Y[i, self.char_indices[c]] = 1

    def encode_into(self, X, C):
        for i, c in enumerate(C):
            if len(X.shape) == 1:
                X[i] = self.char_indices[c]
            elif len(X.shape) == 2:
                X[i, self.char_indices[c]] = 1
            else:
                raise Exception("Code should never reach here, dimension of X can only be 1 or 2")

    def encode(self, C, maxlen=None):
        maxlen = maxlen if maxlen else self.maxlen
        if self.embedding:
            X = np.zeros((maxlen), dtype=np.int8)
        else:
            X = np.zeros((maxlen, len(self.chars)))

        self.encode_into(X, C)
        return X

    def get_char_index(self, character):
        return self.char_indices[character]

    def translate(self, astring):
        return astring

    @staticmethod
    def fromConfig(config):
        if (config.uppercase_character_optimization or
              config.rare_character_optimization):
            return OptimizingCharacterTable(
                config.char_bag, config.context_length,
                config.get_intermediate_info('rare_character_bag'),
                config.uppercase_character_optimization,
                padding_character=config.padding_character,
                embedding=config.embedding_layer)

        return CharacterTable(config.char_bag, config.context_length,
                              padding_character=config.padding_character,
                              embedding=config.embedding_layer,
                              sequence_model=config.sequence_model)

class OptimizingCharacterTable(CharacterTable):
    def __init__(self, chars, maxlen, rare_characters, uppercase,
                 embedding=False, padding_character=None):
        # pylint: disable=too-many-branches
        if uppercase:
            self.rare_characters = ''.join(
                c for c in rare_characters if (
                    c not in string.ascii_uppercase
                    and c not in string.ascii_lowercase))
        else:
            self.rare_characters = rare_characters
        char_bag = chars
        for r in self.rare_characters:
            char_bag = char_bag.replace(r, '')
        if len(rare_characters):
            char_bag += self.rare_characters[0]
            self.rare_dict = {
                char : self.rare_characters[0] for char in self.rare_characters}
            self.rare_character_preimage = {
                self.rare_characters[0] : list(self.rare_characters)}
        else:
            self.rare_character_preimage = {}
            self.rare_dict = {}
        if uppercase:
            for c in string.ascii_uppercase:
                if c not in chars:
                    continue
                self.rare_dict[c] = c.lower()
                char_bag = char_bag.replace(c, '')
                if c.lower() not in char_bag:
                    raise ValueError(
                        "expected %s to be in %s" % (c.lower(), chars))

                self.rare_character_preimage[c.lower()] = [c, c.lower()]
        super().__init__(char_bag, maxlen, embedding, padding_character)
        for key in self.rare_dict:
            self.char_indices[key] = self.char_indices[self.rare_dict[key]]
        translate_table = {}
        for c in chars:
            if c in self.rare_dict:
                translate_table[c] = self.rare_dict[c]
            else:
                translate_table[c] = c

        # pylint: disable=no-member
        #
        # maketrans is a member of the string class
        self.translate_table = ''.maketrans(translate_table)
        self.rare_character_postimage = {}
        for key in self.rare_character_preimage:
            for item in self.rare_character_preimage[key]:
                self.rare_character_postimage[item] = key

    def translate(self, astring):
        return astring.translate(self.translate_table)


class ModelSerializer():
    def __init__(self,
                 archfile=None,
                 weightfile=None,
                 versioned=False,
                 multi_gpu=1):
        self.archfile = archfile
        self.weightfile = weightfile
        self.model_creator_from_json = model_from_json
        self.versioned = versioned
        self.saved_counter = 0
        self.multi_gpu = multi_gpu

    def save_model(self, model):
        if self.archfile is None or self.weightfile is None:
            logging.info(
                'Cannot save model because file arguments were not provided')
            return
        logging.info('Saving model architecture')
        with open(self.archfile, 'w') as arch:
            arch.write(model.to_json())
        logging.info('Saving model weights')
        self.saved_counter += 1
        weight_fname = self.weightfile
        if self.versioned:
            weight_fname += '.' + str(self.saved_counter)
        model.save_weights(weight_fname, overwrite=True)
        logging.info('Done saving model')

    def load_model(self):
        logging.info('Loading model architecture')
        with open(self.archfile, 'r') as arch:
            arch_data = arch.read()
            model = self.model_creator_from_json(arch_data)

        logging.info('Loading model weights')
        model.load_weights(self.weightfile)
        logging.info('Done loading model')

        if self.multi_gpu > 1:
            model = keras.utils.multi_gpu_model(model, gpus=self.multi_gpu)

        return model

class ConfigurationException(Exception):
    pass


def read_config_file(afile):
    _, fileext = os.path.splitext(afile)
    if fileext == '.json':
        file_format = json.load
    elif fileext == '.yaml':
        try:
            import yaml
        except ImportError:
            logging.error(
                'python yaml library is required for yaml configs. '
                'Run: pip install yaml')
            raise

        file_format = yaml.safe_load
    else:
        logging.warning(
            'Unknown config format: %s. Defaulting to JSON', fileext)
        file_format = json.load

    with open(afile, 'r') as f:
        answer = file_format(f)

    return answer


class ModelDefaults():
    char_bag = (
        string.ascii_lowercase + string.ascii_uppercase + string.digits +
        SYMBOLS + PASSWORD_END)
    model_type = 'LSTM'
    sequence_model = Sequence.MANY_TO_ONE
    hidden_size = 128
    layers = 1
    max_len = 40
    min_len = 4
    training_chunk = 128
    generations = 20
    chunk_print_interval = 1000
    lower_probability_threshold = 10**-5
    relevel_not_matching_passwords = True
    training_accuracy_threshold = -1.0
    train_test_ratio = 10
    rare_character_optimization = False
    rare_character_optimization_guessing = False
    uppercase_character_optimization = False
    rare_character_lowest_threshold = 20
    guess_serialization_method = 'human'
    simulated_frequency_optimization = False
    intermediate_fname = MEMORY_ONLY
    save_always = True
    save_model_versioned = False
    randomize_training_order = True
    model_optimizer = 'adam'
    guesser_intermediate_directory = 'guesser_files'
    cleanup_guesser_files = True
    early_stopping = False
    early_stopping_patience = 10000
    compute_stats = False
    password_test_fname = ""
    chunk_size_guesser = 1000
    random_walk_seed_num = 1000
    max_gpu_prediction_size = 25000
    cpu_limit = 8
    random_walk_confidence_bound_z_value = 1.96
    random_walk_confidence_percent = 5
    random_walk_upper_bound = 10
    no_end_word_cache = False
    enforced_policy = 'basic'
    pwd_list_weights = {}
    dropouts = False
    dropout_ratio = .25
    tensorboard = False
    tensorboard_dir = "."
    context_length = None
    train_backwards = False
    dense_layers = 0
    dense_hidden_size = 128
    secondary_training = False
    secondary_train_sets = {}
    training_main_memory_chunksize = 1000000
    probability_steps = False
    freeze_feature_layers_during_secondary_training = True
    secondary_training_save_freqs = False
    guessing_secondary_training = False
    guesser_class = None
    freq_format = 'hex'
    padding_character = False
    convolutional_kernel_size = 3
    embedding_layer = False
    embedding_size = 8
    previous_probability_mapping_file = None
    probability_calculator_cache_size = 0

    def __init__(self, adict=None, **kwargs):
        self.adict = adict if adict is not None else dict()
        for k in kwargs:
            self.adict[k] = kwargs[k]

        if self.context_length is None:
            self.context_length = self.max_len

        if isinstance(self.sequence_model, str):
            self.sequence_model = Sequence[self.sequence_model]

        self._read_intermediate_data_time = None
        self._intermediate_data = self._read_intermediate_data()

    def _read_intermediate_data(self):
        self._read_intermediate_data_time = time.time()
        if self.intermediate_fname == MEMORY_ONLY:
            return {}

        if not os.path.exists(self.intermediate_fname):
            return {}

        with open(self.intermediate_fname, 'r') as info:
            info_as_str = info.read()
            if not info_as_str:
                return {}

            return json.loads(info_as_str)

    def _write_intermediate_data(self):
        if self.intermediate_fname == MEMORY_ONLY:
            return

        self._read_intermediate_data_time = time.time()
        with open(self.intermediate_fname, 'w') as info:
            json.dump(self._intermediate_data, info)

    def _check_if_should_reload(self):
        if self.intermediate_fname == MEMORY_ONLY:
            return

        if not os.path.exists(self.intermediate_fname):
            return

        mod_time = os.path.getmtime(self.intermediate_fname)
        assert self._read_intermediate_data_time is not None
        if mod_time >= self._read_intermediate_data_time:
            self._intermediate_data = self._read_intermediate_data()

    def __getattribute__(self, name):
        if name != 'adict' and name in self.adict:
            return self.adict[name]

        return super().__getattribute__(name)

    def __setattr__(self, name, value):
        if name != 'adict' and not name.startswith("_"):
            self.adict[name] = value
        else:
            super().__setattr__(name, value)

    @staticmethod
    def fromFile(afile):
        if afile is None:
            return ModelDefaults()

        return ModelDefaults(read_config_file(afile))

    def validate(self):
        if self.max_len > 255:
            raise ConfigurationException('expected max_len <= 255')

        if self.password_test_fname:
            if not os.path.exists(self.password_test_fname):
                raise ConfigurationException(
                    'password_test_fname with value "%s" does not exist' %
                    self.password_test_fname)

        if self.rare_character_optimization_guessing:
            if not (self.rare_character_optimization or
                    self.uppercase_character_optimization):
                raise ConfigurationException(
                    'rare_character_optimization_guessing should not be true '
                    'when neither uppercase_character_optimization or '
                    'uppercase_character_optimization are true')

        elif (self.rare_character_optimization or
              self.uppercase_character_optimization):
            logging.warning(
                'Without rare_character_optimization_guessing setting,'
                ' output guesses may ignore case or special characters')

        if self.embedding_layer:
            if not self.embedding_size:
                raise ConfigurationException(
                    'Expected embedding_size when using embedding_layer=True')

        if self.guess_serialization_method not in serializer_type_list:
            raise ConfigurationException(
                'unknown guess_serialization_method: %s' %
                self.guess_serialization_method)

        if self.context_length > self.max_len:
            raise ConfigurationException('Expected context_length <= max_len')

        if self.training_main_memory_chunksize <= self.training_chunk:
            raise ConfigurationException(
                'Expected training_main_memory_chunksize > training_chunk')

        if self.guessing_secondary_training:
            if ((not self.secondary_training) or
                (not self.secondary_training_save_freqs)):
                raise ConfigurationException(
                    'Expected secondary_training and secondary_training_save_freqs')
        if self.sequence_model != Sequence.MANY_TO_MANY and\
                self.sequence_model != Sequence.MANY_TO_ONE:
            raise ConfigurationException(
                "Configuration parameter 'sequence_model' can only be "
                "'many_to_many' or 'many_to_one'")

    def as_dict(self):
        answer = dict(vars(ModelDefaults).copy())
        answer.update(self.adict)
        return {
            key: value for key, value in answer.items() if (
                key[0] != '_' and not hasattr(value, '__call__')
                and not isinstance(value, staticmethod))}

    def set_intermediate_info(self, key, value):
        self._intermediate_data[key] = value
        self._write_intermediate_data()

    def get_intermediate_info(self, key):
        self._check_if_should_reload()
        try:
            return self._intermediate_data[key]
        except KeyError as e:
            logging.error('Cannot find intermediate data %s. Looking in %s',
                          str(e), self.intermediate_fname)
            raise

    def override_from_commandline(self, cmdline):
        answer = {}
        for keyval in cmdline.split(';'):
            if not keyval:
                continue
            key, _, value = keyval.partition('=')
            answer[key] = type(getattr(self, key))(value)
        self.adict.update(answer)

    def sequence_model_updates(self):
        if self.sequence_model == Sequence.MANY_TO_MANY:
            self.char_bag += PASSWORD_START

class BasePreprocessor():
    def __init__(self, config=ModelDefaults()):
        self.config = config

    def begin(self, pwd_list):
        raise NotImplementedError()

    def begin_resetable(self, resetable):
        self.begin(resetable.create_new())

    def next_chunk(self):
        raise NotImplementedError()

    def reset(self):
        raise NotImplementedError()

    def stats(self):
        self.reset()
        x_vec, _, _ = self.next_chunk()
        count_instances = 0
        while len(x_vec) != 0:
            count_instances += len(x_vec)
            x_vec, _, _ = self.next_chunk()
        logging.info('Number of training instances %s', count_instances)
        return count_instances

    @staticmethod
    def fromConfig(config):
        if config.sequence_model == Sequence.MANY_TO_MANY:
            return ManyToManyPreprocessor(config)

        if config.sequence_model == Sequence.MANY_TO_ONE:
            return Preprocessor(config)

        raise ValueError('unknown sequence model: %s' % config.sequence_model)


class Preprocessor(BasePreprocessor):
    def __init__(self, config=ModelDefaults()):
        super().__init__(config)
        self.chunk = 0
        self.resetable_pwd_list = None
        self.pwd_whole_list = None
        self.pwd_freqs = None
        self.chunked_pwd_list = None

    def begin(self, pwd_list):
        self.pwd_whole_list = list(pwd_list)

    def begin_resetable(self, resetable):
        self.resetable_pwd_list = resetable
        self.reset()

    def all_prefixes(self, pwd):
        return [pwd[:i] for i in range(len(pwd))] + [pwd]

    def all_suffixes(self, pwd):
        return [pwd[i] for i in range(len(pwd))] + [PASSWORD_END]

    def repeat_weight(self, pwd):
        return [self.password_weight(pwd) for _ in range(len(pwd) + 1)]

    def train_from_pwds(self, pwd_tuples):
        self.pwd_freqs = dict(pwd_tuples)
        pwds = list(map(lambda x: x[0], pwd_tuples))
        return (itertools.chain.from_iterable(map(self.all_prefixes, pwds)),
                itertools.chain.from_iterable(map(self.all_suffixes, pwds)),
                itertools.chain.from_iterable(map(self.repeat_weight, pwds)))

    def next_chunk(self):
        if self.chunk * self.config.training_chunk >= len(self.pwd_whole_list):
            if self.resetable_pwd_list is None:
                return [], [], []
            try:
                new_iterator = self.chunked_pwd_list.__next__()
            except StopIteration:
                return [], [], []
            self.begin(new_iterator)
            self.reset_subiterator()
            return self.next_chunk()
        pwd_list = self.pwd_whole_list[
            self.chunk * self.config.training_chunk:
            min((self.chunk + 1) * self.config.training_chunk,
                len(self.pwd_whole_list))]
        self.chunk += 1
        pwd_input, output, weight = self.train_from_pwds(pwd_list)
        return (list(pwd_input), list(output), list(weight))

    def password_weight(self, pwd):
        if isinstance(pwd, tuple):
            pwd = ''.join(pwd)

        if pwd in self.pwd_freqs:
            return self.pwd_freqs[pwd]

        assert False, 'Cannot find frequency for password'
        return 0.0

    def reset(self):
        if self.resetable_pwd_list is None:
            self.reset_subiterator()
            return
        self.chunked_pwd_list = iter(grouper(
            self.resetable_pwd_list.as_iterator(),
            self.config.training_main_memory_chunksize))
        try:
            self.begin(self.chunked_pwd_list.__next__())
        except StopIteration:
            logging.warning('Password list has no passwords?')
            self.pwd_whole_list = []

        self.reset_subiterator()

    def reset_subiterator(self):
        self.chunk = 0
        if self.config.randomize_training_order:
            random.shuffle(self.pwd_whole_list)


class Trainer():
    def __init__(self, pwd_list, config=ModelDefaults(), multi_gpu=1):
        self.config = config
        self.chunk = 0
        self.generation = 0
        self.model = None
        self.model_to_save = None
        self.multi_gpu = multi_gpu
        self.pwd_list = pwd_list
        self.cumulative_chunks = 0
        self.min_loss_early_stopping = float("inf")
        self.poor_batches_early_stopping = 0
        self.smoothened_loss = collections.deque(maxlen=int(config.chunk_print_interval))
        if config.tensorboard:
            self.callback = TensorBoard(config.tensorboard_dir)
            self.train_log_names = []
            self.test_log_names = []
        else:
            self.callback = None
            self.train_log_names = None
            self.test_log_names = None

        self.ctable = CharacterTable.fromConfig(self.config)
        self.feature_layers = []
        self.classification_layers = []

    def next_train_set_as_np(self):
        x_strs, y_str_list, weight_list = self.pwd_list.next_chunk()
        x_vec = self.prepare_x_data(x_strs)
        y_vec = self.prepare_y_data(y_str_list)
        weight_vec = np.zeros((len(weight_list)))
        for i, weight in enumerate(weight_list):
            weight_vec[i] = weight
        return shuffle(x_vec, y_vec, weight_vec)

    def prepare_x_data(self, x_strs):
        return self.ctable.encode_many(x_strs)

    def prepare_y_data(self, y_str_list):
        y_vec = np.zeros((len(y_str_list), self.ctable.vocab_size),
                         dtype=np.bool)
        self.ctable.y_encode_into(y_vec, y_str_list)
        return y_vec

    def _make_layer(self, **kwargs):
        recurrent_train_backwards = self.config.train_backwards
        model_type = self.config.model_type
        hidden_size = self.config.hidden_size
        if model_type == 'GRU':
            return recurrent.GRU(
                hidden_size,
                return_sequences=True,
                go_backwards=recurrent_train_backwards,
                **kwargs)

        if model_type == 'LSTM':
            return recurrent.LSTM(
                hidden_size,
                return_sequences=True,
                go_backwards=recurrent_train_backwards,
                **kwargs)

        if model_type == 'Conv1D':
            return Conv1D(
                hidden_size,
                self.config.convolutional_kernel_size,
                **kwargs)

        raise ConfigurationException('Unknown model_type: %s' % model_type)

    def _return_model(self):
        model = Sequential()

        # Add the first input layer. If embedding is enabled, we add a different
        # layer which does not have the input_shape defined
        if self.config.embedding_layer:
            self.feature_layers.append(
                Embedding(
                    self.ctable.vocab_size,
                    self.config.embedding_size,
                    input_length=self.config.context_length))

            self.feature_layers.append(self._make_layer())
        else:
            self.feature_layers.append(
                self._make_layer(
                    input_shape=(
                        self.config.context_length, self.ctable.vocab_size)))

        # Add the main model layers. These layers will not be trainable during
        # secondary training.
        for _ in range(self.config.layers):
            if self.config.dropouts:
                self.feature_layers.append(Dropout(self.config.dropout_ratio))

            self.feature_layers.append(self._make_layer())

        self.feature_layers.append(Flatten())

        # Add any additional classification layers. These layers may be
        # trainable during secondary training.
        for _ in range(self.config.dense_layers):
            self.classification_layers.append(
                Dense(self.config.dense_hidden_size))

        # Append the final layer which has the correct dimensions for the output
        self.classification_layers.append(
            Dense(self.ctable.vocab_size, activation='softmax'))

        # Actually build the model
        for layer in self.feature_layers + self.classification_layers:
            try:
                model.add(layer)
            except Exception as e:
                logging.error('Error when adding layer %s: %s', layer, e)
                raise

        return model

    def build_model(self, model=None):

        if self.multi_gpu >= 2:
            with tf.device('/cpu:0'):
                if model is None:
                    model = self._return_model()
                self.model_to_save = model
            model = keras.utils.multi_gpu_model(model, gpus=self.multi_gpu)
        else:
            if model is None:
                model = self._return_model()
            self.model_to_save = model
        metrics = ['accuracy']

        if self.config.tensorboard:
            tensorboard_metrics = ['loss'] + metrics
            self.train_log_names = ['train_' + name for name in tensorboard_metrics]
            self.test_log_names = ['test_' + name for name in tensorboard_metrics]

        model.compile(loss='categorical_crossentropy',
                      optimizer=self.config.model_optimizer,
                      metrics=metrics)
        self.model = model

    def init_layers(self):
        assert self.model is not None
        assert len(self.classification_layers) == 0
        assert len(self.feature_layers) == 0
        for layer in self.model.layers:
            if isinstance(layer, (TimeDistributed, Activation, Dense)):
                self.classification_layers.append(layer)
            else:
                self.feature_layers.append(layer)

    def train_model(self, serializer):
        prev_accuracy = 0
        max_accuracy = 0
        if self.config.tensorboard:
            self.callback.set_model(self.model)

        for gen in range(self.config.generations):
            self.generation = gen + 1
            logging.info('Generation %d', gen + 1)
            accuracy, early_stop = self.train_model_generation(serializer)
            logging.info('Generation accuracy: %s', accuracy)
            if early_stop:
                break
            if not self.config.early_stopping and \
                    (accuracy > max_accuracy or self.config.save_always):
                max_accuracy = accuracy
                serializer.save_model(self.model_to_save)
            if ((accuracy - prev_accuracy) <
                self.config.training_accuracy_threshold):
                logging.info('Accuracy diff of %s is less than threshold.',
                             accuracy - prev_accuracy)
                break
            prev_accuracy = accuracy

    def test_set(self, x_all, y_all, w_all):
        split_at = len(x_all) - max(
            int(len(x_all) / self.config.train_test_ratio), 1)
        x_train = x_all[0:split_at, :]
        x_val = x_all[split_at:, :]
        y_train, y_val = (y_all[:split_at], y_all[split_at:])
        w_train, w_val = (w_all[:split_at], w_all[split_at:])
        return x_train, x_val, y_train, y_val, w_train, w_val

    def training_step(self, x_all, y_all, w_all):
        x_train, x_val, y_train, y_val, w_train, w_val = self.test_set(
            x_all, y_all, w_all)
        train_loss, train_accuracy = self.model.train_on_batch(
            x_train, y_train, sample_weight=w_train)
        test_loss, test_accuracy = self.model.test_on_batch(
            x_val, y_val, sample_weight=w_val)
        return (train_loss, train_accuracy, test_loss, test_accuracy)

    def early_stopping(self, smooth_loss, serializer):
        stop = False
        if self.min_loss_early_stopping > smooth_loss:
            self.min_loss_early_stopping = smooth_loss
            serializer.save_model(self.model_to_save)
            self.poor_batches_early_stopping = 0
        elif self.poor_batches_early_stopping < self.config.early_stopping_patience:
            self.poor_batches_early_stopping += 1
        else:
            stop = True
        return stop

    def train_model_generation(self, serializer=None):
        if self.config.early_stopping:
            assert serializer, "Need to specify serializer with early_stopping"
        self.chunk = 0
        self.pwd_list.reset()
        accuracy_accum = []
        x_all, y_all, w_all = self.next_train_set_as_np()
        chunk = 0
        early_stop = False
        while len(x_all) != 0:
            assert len(x_all) == len(y_all)
            tr_loss, tr_acc, te_loss, te_acc = self.training_step(
                x_all, y_all, w_all)
            accuracy_accum += [(len(x_all), te_acc)]
            self.smoothened_loss.append((len(x_all), te_loss))
            if self.config.tensorboard:
                self.write_log(self.train_log_names, [tr_loss, tr_acc], self.cumulative_chunks)
                self.write_log(self.test_log_names, [te_loss, te_acc], self.cumulative_chunks)

            if chunk % self.config.chunk_print_interval == 0:
                #Finding weighted average to get the right loss value over batches
                # of unequal sizes
                instances_smoothened = map(lambda x: x[0], self.smoothened_loss)
                loss_smoothened = sum(map(lambda x: x[0] * x[1], self.smoothened_loss)
                                      ) / sum(instances_smoothened)
                logging.info('Chunk %s. Each chunk is size %s',
                             chunk, len(x_all))
                logging.info('Train loss %s. Test loss %s. Test accuracy %s. Averaged loss %s',
                             tr_loss, te_loss, te_acc, loss_smoothened)
                if self.config.tensorboard:
                    self.callback.writer.flush()

            if self.config.early_stopping and \
                    self.cumulative_chunks >= self.config.early_stopping_patience and \
                self.cumulative_chunks % self.config.chunk_print_interval == 0:
                #Second condition so that the model doesn't start saving \
                # very early in the training process
                #Third condition to prevent evaluation of accuracy too frequently
                instances_smoothened = map(lambda x: x[0], self.smoothened_loss)
                loss_smoothened = sum(map(lambda x: x[0] * x[1], self.smoothened_loss)
                                      ) / sum(instances_smoothened)
                early_stop = self.early_stopping(loss_smoothened, serializer)
                if early_stop:
                    instances = map(lambda x: x[0], accuracy_accum)
                    return sum(map(lambda x: x[0] * x[1], accuracy_accum)
                               ) / sum(instances), early_stop
            x_all, y_all, w_all = self.next_train_set_as_np()
            chunk += 1
            self.cumulative_chunks += 1
        instances = map(lambda x: x[0], accuracy_accum)
        return sum(map(lambda x: x[0] * x[1], accuracy_accum)) / sum(instances), early_stop

    def train(self, serializer):
        logging.info('Building model...')
        self.build_model(self.model)
        logging.info('Done compiling model. Beginning training...')
        self.train_model(serializer)

    def freeze_feature_layers(self):
        for layer in self.feature_layers:
            layer.trainable = False

    def retrain_classification(self, preprocessor, serializer):
        assert self.model is not None
        assert len(self.feature_layers) != 0
        if self.config.freeze_feature_layers_during_secondary_training:
            logging.info('Freezing feature layers...')
            self.freeze_feature_layers()
        logging.info('Retraining...')
        self.pwd_list = preprocessor
        self.train(serializer)

    def write_log(self, names, logs, batch_no):
        assert self.callback
        for name, value in zip(names, logs):
            summary = tf.Summary()
            summary_value = summary.value.add()
            summary_value.simple_value = value
            summary_value.tag = name
            self.callback.writer.add_summary(summary, batch_no)


class ManyToManyTrainer(Trainer):
    def _return_model(self):
        model = Sequential()

        # Add the first input layer. If embedding is enabled, we add a different
        # layer which does not have the input_shape defined
        if self.config.embedding_layer:
            self.feature_layers.append(
                Embedding(
                    self.ctable.vocab_size,
                    self.config.embedding_size,
                    input_length=self.config.context_length))

            self.feature_layers.append(self._make_layer())
        else:
            self.feature_layers.append(
                self._make_layer(
                    input_shape=(
                        self.config.context_length, self.ctable.vocab_size)))

        # Add the main model layers. These layers will not be trainable during
        # secondary training.
        for _ in range(self.config.layers):
            if self.config.dropouts:
                self.feature_layers.append(Dropout(self.config.dropout_ratio))
            self.feature_layers.append(self._make_layer())

        # Add any additional classification layers. These layers may be
        # trainable during secondary training.
        for _ in range(self.config.dense_layers):
            self.classification_layers.append(TimeDistributed(Dense(
                self.config.hidden_size)))

        self.classification_layers.append(TimeDistributed(Dense(
            self.ctable.vocab_size, activation="softmax")))

        # Actually build the model
        for layer in self.feature_layers + self.classification_layers:
            try:
                model.add(layer)
            except Exception as e:
                logging.error('Error when adding layer %s: %s', layer, e)
                raise

        return model

    def next_train_set_as_np(self):
        x_strs, y_str_list, weight_list = self.pwd_list.next_chunk()
        x_vec = self.prepare_x_data(x_strs)
        y_vec = self.prepare_y_data(y_str_list)
        weight_vec = np.zeros((len(weight_list)))
        for i, weight in enumerate(weight_list):
            weight_vec[i] = weight
        return shuffle(x_vec, y_vec, weight_vec)

    def prepare_y_data(self, y_str_list):
        y_vec = self.ctable.encode_many(y_str_list, y_vec=True)
        return y_vec

class ManyToManyPreprocessor(Preprocessor):
    def all_prefixes(self, pwd):
        return [PASSWORD_START + pwd]

    def all_suffixes(self, pwd):
        return [pwd + PASSWORD_END]

    def repeat_weight(self, pwd):
        return [self.password_weight(pwd)]

class PwdList():
    class NoListTypeException(Exception):
        pass

    def __init__(self, ifile_name):
        self.ifile_name = ifile_name

    def as_list_iter(self, agen):
        for row in agen:
            yield (row.strip(PASSWORD_END), 1)

    def as_list(self):
        if self.ifile_name[-3:] == '.gz':
            with gzip.open(self.ifile_name, 'rt') as ifile:
                for item in self.as_list_iter(ifile):
                    yield item
        else:
            with open(self.ifile_name, 'r') as ifile:
                for item in self.as_list_iter(ifile):
                    yield item

    def finish(self):
        pass

    @staticmethod
    def getFactory(file_formats, config):
        assert isinstance(file_formats, list)
        if len(file_formats) > 1:
            return lambda flist: ConcatenatingList(config, flist, file_formats)
        assert len(file_formats) > 0
        if file_formats[0] == 'tsv':
            if config.simulated_frequency_optimization:
                return lambda flist: TsvSimulatedList(
                    flist[0], freq_format_hex=(config.freq_format == 'hex'))

            return lambda flist: TsvList(flist[0])

        if file_formats[0] == 'list':
            return lambda flist: PwdList(flist[0])

        raise PwdList.NoListTypeException(
            'Cannot find factory for format of %s' % file_formats)

class TsvListParent(PwdList):
    def __init__(self, fname, freq_format_hex=True):
        self.freq_format_hex = freq_format_hex
        self.ctr = 0
        super().__init__(fname)

    def interpret_row(self, row):
        self.ctr += 1
        if len(row) < 2:
            logging.error(
                'Invalid number of tabs on line %d, expected 2 but was %d',
                self.ctr, len(row))
            return None, None
        try:
            if self.freq_format_hex:
                value = float.fromhex(row[1])
            else:
                value = int(row[1])
        except ValueError as e:
            logging.error(
                '%s invalid input format of string "%s" on line %d pwd "%s"',
                str(e), row[1], self.ctr, row[0])
            return None, None
        return row[0], int(value)

class TsvList(TsvListParent):
    def as_list_iter(self, agen):
        for row in csv.reader(iter(agen), delimiter='\t', quotechar=None):
            pwd, freq = self.interpret_row(row)
            if pwd is not None:
                for _ in range(freq):
                    yield (pwd, 1)

class TsvSimulatedList(TsvListParent):
    def as_list_iter(self, agen):
        for row in csv.reader(iter(agen), delimiter='\t', quotechar=None):
            pwd, value = self.interpret_row(row)
            if pwd is not None:
                yield (pwd, value)

class ConcatenatingList():
    CONFIG_IM_KEY = 'empirical_weighting'

    def __init__(self, config, file_list, file_formats):
        assert len(file_list) == len(file_formats)
        self.config = config
        self.file_tuples = zip(file_list, file_formats)
        self.frequencies = collections.defaultdict(int)

    def get_iterable(self, file_tuple):
        file_name, file_format = file_tuple
        input_factory = PwdList.getFactory([file_format], self.config)
        return map(lambda t: t + (file_name,),
                   input_factory([file_name]).as_list())

    def finish(self):
        answer = {}
        if len(self.config.pwd_list_weights.keys()) == 0:
            logging.info('Using equal weighting')
            for key in self.frequencies.keys():
                answer[key] = 1
            self.config.set_intermediate_info(self.CONFIG_IM_KEY, answer)
            return
        try:
            assert (set(self.config.pwd_list_weights.keys()) ==
                    set(self.frequencies.keys()))
        except AssertionError as e:
            logging.critical(
                'Given files do not match config weight files, %s, %s',
                set(self.config.pwd_list_weights.keys()),
                set(self.frequencies.keys()))
            raise e
        logging.info('Password frequencies are: %s', self.frequencies)
        sum_all = 0
        for key in self.frequencies.keys():
            sum_all += self.frequencies[key]
        for key in self.config.pwd_list_weights:
            logging.info('Number of unfiltered passwords in %s = %s',
                         key, self.frequencies[key])
            answer[key] = self.config.pwd_list_weights[key]
        logging.info('Weights are: %s', answer)
        self.config.set_intermediate_info(self.CONFIG_IM_KEY, answer)

    def as_list(self):
        answer = []
        try:
            weighting = self.config.get_intermediate_info(self.CONFIG_IM_KEY)
        except KeyError:
            logging.info('First run through, no weighting')
            weighting = False
        def increment_freqs(pwd_tuple):
            self.frequencies[pwd_tuple[2]] += pwd_tuple[1]
            return pwd_tuple[:2]
        def get_freqs(pwd_tuple):
            return (pwd_tuple[0], pwd_tuple[1] * weighting[pwd_tuple[2]])
        if weighting:
            fn = get_freqs
            logging.info('Using weighting')
        else:
            logging.info(
                'Not using weighting, reading training data for the first time')
            fn = increment_freqs
        for atuple in self.file_tuples:
            logging.info('Reading from %s', atuple)
            answer.append(map(fn, self.get_iterable(atuple)))
        return itertools.chain.from_iterable(answer)

class Filterer():
    def __init__(self, config, uniquify=False):
        self.filtered_out = 0
        self.total = 0
        self.total_characters = 0
        self.frequencies = collections.defaultdict(int)
        self.beg_frequencies = collections.defaultdict(int)
        self.end_frequencies = collections.defaultdict(int)
        self.config = config
        self.longest_pwd = 0
        self.char_bag = config.char_bag
        if config.sequence_model == Sequence.MANY_TO_MANY:
            # Replace so that you don't accept passwords with tab character in them
            self.char_bag = self.char_bag.replace("\t", "")
        self.max_len = config.max_len
        self.min_len = config.min_len
        self.uniquify = uniquify
        self.seen = set()

    @staticmethod
    def inc_frequencies(adict, pwd):
        for c in pwd:
            adict[c] += 1

    def pwd_is_valid(self, pwd, quick=False):
        if isinstance(pwd, tuple):
            pwd = ''.join(pwd)
        pwd = pwd.strip(PASSWORD_END)
        answer = (all(map(lambda c: c in self.char_bag, pwd)) and
                  len(pwd) <= self.max_len and
                  len(pwd) >= self.min_len)
        if self.uniquify:
            if pwd in self.seen:
                answer = False
            else:
                self.seen.add(pwd)
        if quick:
            return answer
        if answer:
            self.total_characters += len(pwd)
            Filterer.inc_frequencies(self.frequencies, pwd)
            Filterer.inc_frequencies(self.beg_frequencies, pwd[0])
            Filterer.inc_frequencies(self.end_frequencies, pwd[-1])
        else:
            self.filtered_out += 1
        self.total += 1
        self.longest_pwd = max(self.longest_pwd, len(pwd))
        return answer

    def rare_characters(self):
        lowest = list(map(
            lambda x: x[0],
            sorted(map(lambda c: (c, self.frequencies[c]),
                       self.config.char_bag.replace(PASSWORD_END, '')),
                   key=lambda x: x[1])))
        return lowest[:min(self.config.rare_character_lowest_threshold,
                           len(lowest))]

    def finish(self, save_stats=True, save_freqs=True):
        logging.info('Filtered %s of %s passwords',
                     self.filtered_out, self.total)
        char_freqs = {}
        for key in self.frequencies:
            char_freqs[key] = self.frequencies[key] / self.total_characters

        if save_stats:
            self.config.set_intermediate_info(
                'rare_character_bag', self.rare_characters())
            logging.info('Rare characters: %s', self.rare_characters())
            logging.info('Longest pwd is : %s characters long',
                         self.longest_pwd)

        if save_freqs:
            self.config.set_intermediate_info(
                'character_frequencies', self.frequencies)
            self.config.set_intermediate_info(
                'beginning_character_frequencies', self.beg_frequencies)
            self.config.set_intermediate_info(
                'end_character_frequencies', self.end_frequencies)

    def filter(self, alist, quick=False):
        return filter(lambda x: self.pwd_is_valid(x[0], quick=quick), alist)

class ResetablePwdList():
    def __init__(self, pwd_file, pwd_format, config):
        self.pwd_file = pwd_file
        self.pwd_format = pwd_format
        self.config = config

    def create_new(self, quick=False):
        return Filterer(self.config).filter(
            PwdList.getFactory(
                self.pwd_format, self.config)(self.pwd_file).as_list(),
            quick=quick)

    def initialize(self, save_stats=True, save_freqs=True):
        input_factory = PwdList.getFactory(self.pwd_format, self.config)
        filt = Filterer(self.config)
        logging.info('Reading training set...')
        input_list = input_factory(self.pwd_file)
        for _ in filt.filter(input_list.as_list()):
            pass
        filt.finish(save_stats=save_stats, save_freqs=save_freqs)
        input_list.finish()
        logging.info('Done reading passwords...')

    def as_iterator(self, quick=False):
        return (pwd for pwd in self.create_new(quick))

class GuessSerializer():
    TOTAL_COUNT_RE = re.compile('Total count: (\\d*)\n')
    TOTAL_COUNT_FORMAT = 'Total count: %s\n'

    def __init__(self, ostream):
        self.ostream = ostream
        self.total_guessed = 0

    def serialize(self, password, prob):
        if prob == 0:
            return
        if isinstance(password, tuple):
            password = ''.join(password)
        self.total_guessed += 1
        self.ostream.write('%s\t%s\n' % (password, prob))

    def get_total_guessed(self):
        return self.total_guessed

    def collect_answer(self, real_output, istream):
        for line in istream:
            real_output.write(line)

    def finish_collecting(self, real_output):
        logging.info('Finishing aggregating child output')
        real_output.flush()

    def get_stats(self):
        raise NotImplementedError()

    def finish(self):
        self.ostream.flush()
        self.ostream.close()

class DelegatingSerializer():
    def __init__(self, serializer):
        self.serializer = serializer

    def finish_collecting(self, real_output):
        self.serializer.finish_collecting(real_output)

    def collect_answer(self, real_output, istream):
        self.serializer.collect_answer(real_output, istream)

    def get_total_guessed(self):
        return self.serializer.get_total_guessed()

    def get_stats(self):
        return self.serializer.get_stats()

    def finish(self):
        self.serializer.finish()

class GuessNumberGenerator(GuessSerializer):
    def __init__(self, ostream, pwd_list):
        super().__init__(ostream)
        self.pwds, self.probs = zip(*sorted(pwd_list, key=lambda x: x[1]))
        self.guess_numbers = [0] * len(self.pwds)
        self.collected_freqs = collections.defaultdict(int)
        self.collected_probs = {}
        self.collected_total_count = 0

    def serialize(self, _, prob):
        if prob == 0:
            return
        self.total_guessed += 1
        idx = bisect.bisect_left(self.probs, prob) - 1
        if idx >= 0:
            self.guess_numbers[idx] += 1

    def collect_answer(self, real_output, istream):
        lineOne = istream.readline()
        total_count = int(self.TOTAL_COUNT_RE.match(lineOne).groups(0)[0])
        self.collected_total_count += total_count
        for row in csv.reader(istream, delimiter='\t', quotechar=None):
            pwd, prob, freq = row
            freq_num = int(freq)
            if pwd in self.collected_probs:
                assert self.collected_probs[pwd] == prob
            else:
                self.collected_probs[pwd] = prob
            if freq_num >= total_count:
                continue
            self.collected_freqs[pwd] += freq_num

    def write_to_file(self, ostream, total_count, get_freq):
        ostream.write(self.TOTAL_COUNT_FORMAT % total_count)
        writer = csv.writer(ostream, delimiter='\t', quotechar=None)
        for i in range(len(self.pwds), 0, -1):
            idx = i - 1
            writer.writerow([
                self.pwds[idx], self.probs[idx], get_freq(idx)])
        ostream.flush()

    def finish_collecting(self, real_output):
        def get_pwd_freq(idx):
            pwd = self.pwds[idx]
            if pwd in self.collected_freqs:
                return self.collected_freqs[pwd]

            return self.collected_total_count

        logging.info('Finishing collecting answers')
        self.write_to_file(
            real_output, self.collected_total_count, get_pwd_freq)

    def finish(self):
        for i in range(len(self.guess_numbers) - 1, 0, -1):
            self.guess_numbers[i - 1] += self.guess_numbers[i]
        logging.info('Guessed %s passwords', self.total_guessed)
        self.write_to_file(self.ostream, self.total_guessed,
                           lambda idx: self.guess_numbers[idx])
        self.ostream.close()

    def get_stats(self):
        raise NotImplementedError()

class ProbabilityCalculator():
    def __init__(self, guesser, prefixes=False, cache_size=0):
        self.guesser = guesser
        self.ctable = CharacterTable.fromConfig(guesser.config)
        self.config = guesser.config
        self.preproc = BasePreprocessor.fromConfig(self.config)
        self.template_probs = False
        self.prefixes = prefixes
        self._cache_size = cache_size
        if cache_size > 0:
            self._prob_batch_cache = pylru.lrucache(cache_size)
        else:
            self._prob_batch_cache = None

        if guesser.should_make_guesses_rare_char_optimizer:
            self.template_probs = True
            self.pts = PasswordTemplateSerializer(guesser.config)

    def _cached_batch_prob(self, x_strings):
        if self._cache_size == 0:
            return self.guesser.batch_prob(x_strings)

        idx_mapping = []
        answers = [-1] * len(x_strings)
        query_x_strings = []
        for i, x_str in enumerate(x_strings):
            if x_str in self._prob_batch_cache:
                answers[i] = self._prob_batch_cache[x_str]
            else:
                idx_mapping.append(i)
                query_x_strings.append(x_str)

        if query_x_strings:
            nn_response = self.guesser.batch_prob(query_x_strings)
            for i, nn_answer in enumerate(nn_response):
                query_idx = idx_mapping[i]
                query = x_strings[query_idx]
                self._prob_batch_cache[query] = nn_answer
                answers[query_idx] = nn_answer

        return answers

    def probability_stream(self, pwd_list):
        self.preproc.begin(pwd_list)
        x_strings, y_strings, _ = self.preproc.next_chunk()
        while len(x_strings) != 0:
            y_indices = list(map(self.ctable.get_char_index, y_strings))
            probs = self._cached_batch_prob(x_strings)
            assert len(probs) == len(x_strings)
            for i, y_idx in enumerate(y_indices):
                prob = np.asscalar(probs[i][0][y_idx])
                yield x_strings[i], y_strings[i], prob

            x_strings, y_strings, _ = self.preproc.next_chunk()

    def calc_probabilities(self, pwd_list):
        prev_prob = 1
        for item in self.probability_stream(pwd_list):
            input_string, next_char, output_prob = item
            if next_char != PASSWORD_END or self.prefixes is False:
                prev_prob *= output_prob
            if next_char == PASSWORD_END:
                if self.template_probs:
                    prev_prob *= self.pts.find_real_pwd(
                        self.ctable.translate(input_string), input_string)
                yield (input_string, prev_prob)
                prev_prob = 1

        self.preproc.reset()

class ManyToManyProbabilityCalculator(ProbabilityCalculator):
    def probability_stream(self, pwd_list):
        self.preproc.begin(pwd_list)
        x_strings, y_strings, _ = self.preproc.next_chunk()
        logging.debug('Initial probabilities: %s, %s', x_strings, y_strings)
        while len(x_strings) != 0:
            probs = self.guesser.batch_prob(x_strings)
            y_indices, y_strings = self.ctable.encode_many_chunks(y_strings,
                                                                  self.config.max_len, y_vec=True)
            _, x_strings = self.ctable.encode_many_chunks(x_strings, self.config.max_len)
            assert len(probs) == len(y_indices) == len(y_strings) == len(x_strings)
            for i, _ in enumerate(y_strings):
                for j, y_idx in enumerate(y_indices[i]):
                    if j == len(x_strings[i]):
                        break
                    yield x_strings[i], y_strings[i][j], np.asscalar(probs[i][j][y_idx])
            x_strings, y_strings, _ = self.preproc.next_chunk()

class PasswordTemplateSerializer(DelegatingSerializer):
    def __init__(self, config, serializer=None, lower_prob_threshold=None):
        super().__init__(serializer)
        ctable = CharacterTable.fromConfig(config)
        assert isinstance(ctable, OptimizingCharacterTable)
        self.preimage = ctable.rare_character_preimage
        self.char_frequencies = config.get_intermediate_info(
            'character_frequencies')
        self.beginning_char_frequencies = config.get_intermediate_info(
            'beginning_character_frequencies')
        self.end_char_frequencies = config.get_intermediate_info(
            'end_character_frequencies')
        self.lower_probability_threshold = (
            config.lower_probability_threshold
            if lower_prob_threshold is None else lower_prob_threshold)
        self.beg_cache = self.cache_freqs(self.beginning_char_frequencies)
        self.end_cache = self.cache_freqs(self.end_char_frequencies)
        self.cache = self.cache_freqs(self.char_frequencies)
        if config.no_end_word_cache:
            self.end_cache = self.cache
        self.chars = config.char_bag
        self.char_indices = ctable.char_indices
        self.post_image = ctable.rare_character_postimage
        self.make_expander_cache()

    def lookup_in_cache(self, cache, template_char, character):
        return cache[template_char][character]

    def cache_freqs(self, freqs):
        answer = collections.defaultdict(dict)
        for template_char in self.preimage:
            for preimage in self.preimage[template_char]:
                answer[template_char][preimage] = self._calc(
                    freqs, template_char, preimage)
        return answer

    def _calc(self, freqs, template_char, character):
        return freqs[character] / sum(map(
            lambda c: freqs[c], self.preimage[template_char]))

    def calc(self, template_char, character, begin=False, end=False):
        if begin:
            return self.lookup_in_cache(
                self.beg_cache, template_char, character)
        if end:
            return self.lookup_in_cache(
                self.end_cache, template_char, character)
        return self.lookup_in_cache(
            self.cache, template_char, character)

    def make_expander_cache(self):
        self.expander_cache = [0] * len(self.chars)
        for i, after_image_char in enumerate(self.chars):
            if after_image_char not in self.post_image:
                self.expander_cache[i] = self.char_indices[after_image_char]
            else:
                self.expander_cache[i] = self.char_indices[
                    self.post_image[after_image_char]]
        self.expander_cache = np.array(self.expander_cache)
        self.post_image_idx = []
        for i, after_image_char in enumerate(self.chars):
            if after_image_char in self.post_image:
                self.post_image_idx.append((
                    i, after_image_char, self.post_image[after_image_char]))

    def expand_conditional_probs(self, probs, context):
        return self.expand_conditional_probs_cache(
            probs, len(context) == 0, self.expander_cache)

    def expand_conditional_probs_cache(self, probs, context, expander_cache):
        answer = probs[expander_cache]
        for i, after_image_char, post_image in self.post_image_idx:
            answer[i] *= self.calc(post_image, after_image_char, context)
        return answer

    def find_real_pwd(self, template, pwd):
        if isinstance(pwd, tuple):
            pwd = ''.join(pwd)
        assert len(pwd) == len(template)
        prob = 1
        for i, char in enumerate(template):
            if char in self.preimage:
                preimages = self.preimage[char]
                assert pwd[i] in preimages
                prob *= self.calc(
                    char, pwd[i], i == 0, i == (len(template) - 1))
        return prob

    def recursive_helper(self, cur_template, cur_pwd, cur_prob):
        if cur_prob < self.lower_probability_threshold:
            return
        if len(cur_template) == 0:
            self.serializer.serialize(cur_pwd, cur_prob)
            return
        if cur_template[0] in self.preimage:
            preimages = self.preimage[cur_template[0]]
            for c in preimages:
                recur_pwd = cur_pwd + c
                self.recursive_helper(
                    cur_template[1:], recur_pwd,
                    cur_prob * self.calc(cur_template[0], c,
                                         len(cur_pwd) == 0,
                                         len(cur_template) == 1))
        else:
            self.recursive_helper(
                cur_template[1:], cur_pwd + cur_template[0], cur_prob)

    def serialize(self, pwd_template, prob):
        self.recursive_helper(pwd_template, '', prob)

# Initialized later
policy_list = {}

class BasePasswordPolicy():
    def pwd_complies(self, pwd):
        raise NotImplementedError()

    @staticmethod
    def fromConfig(config):
        return policy_list[config.enforced_policy]

class BasicPolicy(BasePasswordPolicy):
    def pwd_complies(self, pwd):
        return True

class PasswordPolicy(BasePasswordPolicy):
    def __init__(self, regexp):
        self.re = re.compile(regexp)

    def pwd_complies(self, pwd):
        return self.re.match(pwd) is not None

class ComplexPasswordPolicy(BasePasswordPolicy):
    digits = set(string.digits)
    uppercase = set(string.ascii_uppercase)
    lowercase = set(string.ascii_lowercase)
    upper_and_lowercase = set(string.ascii_uppercase + string.ascii_lowercase)
    non_symbols = set(
        string.digits + string.ascii_uppercase + string.ascii_lowercase)

    def __init__(self, required_length=8):
        self.blacklist = set()
        self.required_length = required_length

    def load_blacklist(self, fname):
        with open(fname, 'r') as blacklist:
            for line in blacklist:
                self.blacklist.add(line.strip('\n'))

    def has_group(self, pwd, group):
        return any(map(lambda c: c in group, pwd))

    def all_from_group(self, pwd, group):
        return all(map(lambda c: c in group, pwd))

    def passes_blacklist(self, pwd):
        return (''.join(filter(
            lambda c: c in self.upper_and_lowercase, pwd)).lower()
                not in self.blacklist)

    def pwd_complies(self, pwd):
        pwd = pwd.strip(PASSWORD_END)
        if len(pwd) < self.required_length:
            return False
        if not self.has_group(pwd, self.digits):
            return False
        if not self.has_group(pwd, self.uppercase):
            return False
        if not self.has_group(pwd, self.lowercase):
            return False
        if self.all_from_group(pwd, self.non_symbols):
            return False
        return self.passes_blacklist(pwd)

class ComplexPasswordPolicyLowercase(ComplexPasswordPolicy):
    def pwd_complies(self, pwd):
        pwd = pwd.strip(PASSWORD_END)
        if len(pwd) < self.required_length:
            return False
        if not self.has_group(pwd, self.digits):
            return False
        if not self.has_group(pwd, self.upper_and_lowercase):
            return False
        if self.all_from_group(pwd, self.non_symbols):
            return False
        return self.passes_blacklist(pwd)

class OneUppercasePolicy(ComplexPasswordPolicy):
    def pwd_complies(self, pwd):
        pwd = pwd.strip(PASSWORD_END)
        if len(pwd) < self.required_length:
            return False
        if not self.has_group(pwd, self.uppercase):
            return False
        return self.passes_blacklist(pwd)

class SemiComplexPolicyLowercase(ComplexPasswordPolicy):
    def pwd_complies(self, pwd):
        pwd = pwd.strip(PASSWORD_END)
        count = 0
        if len(pwd) < self.required_length:
            return False
        if self.has_group(pwd, self.digits):
            count += 1
        if self.has_group(pwd, self.upper_and_lowercase):
            count += 1
        if self.all_from_group(pwd, self.non_symbols):
            count += 1
        return self.passes_blacklist(pwd) and count >= 2

class SemiComplexPolicy(ComplexPasswordPolicy):
    def pwd_complies(self, pwd):
        pwd = pwd.strip(PASSWORD_END)
        count = 0
        if len(pwd) < self.required_length:
            return False
        if self.has_group(pwd, self.digits):
            count += 1
        if self.has_group(pwd, self.uppercase):
            count += 1
        if self.has_group(pwd, self.lowercase):
            count += 1
        if not self.all_from_group(pwd, self.non_symbols):
            count += 1
        return self.passes_blacklist(pwd) and count >= 3

policy_list = {
    'complex' : ComplexPasswordPolicy(),
    'basic' : BasicPolicy(),
    '1class8' : PasswordPolicy('.{8,}'),
    'basic_long' : PasswordPolicy('.{16,}'),
    'complex_lowercase' : ComplexPasswordPolicyLowercase(),
    'complex_long' : ComplexPasswordPolicy(16),
    'complex_long_lowercase' : ComplexPasswordPolicyLowercase(16),
    'semi_complex' : SemiComplexPolicy(12),
    'semi_complex_lowercase' : SemiComplexPolicyLowercase(12),
    '3class12' : SemiComplexPolicy(12),
    '2class12_all_lowercase' : SemiComplexPolicyLowercase(12),
    'one_uppercase' : OneUppercasePolicy(3)
}


class PasswordPolicyEnforcingSerializer(DelegatingSerializer):
    def __init__(self, policy, serializer):
        super().__init__(serializer)
        self.policy = policy

    def serialize(self, pwd, prob):
        if self.policy.pwd_complies(pwd):
            self.serializer.serialize(pwd, prob)
        else:
            self.serializer.serialize(pwd, 0)


class Guesser():
    def __init__(self, model, config, ostream=None):
        self.model = model
        self.config = config
        self.max_len = config.max_len
        self.char_bag = config.char_bag
        self.max_gpu_prediction_size = config.max_gpu_prediction_size
        self.lower_probability_threshold = config.lower_probability_threshold
        self.relevel_not_matching_passwords = (
            config.relevel_not_matching_passwords)
        self.generated = 0
        self.ctable = CharacterTable.fromConfig(self.config)
        self.filterer = Filterer(self.config)
        self.chunk_size_guesser = self.config.chunk_size_guesser
        self.ostream = ostream
        self.chars_list = self.ctable.char_list
        self._calc_prob_cache = None
        self.should_make_guesses_rare_char_optimizer = (
            self._should_make_guesses_rare_char_optimizer())
        self.output_serializer = self.make_serializer()
        self.pwd_end_idx = self.chars_list.index(PASSWORD_END)

    def read_test_passwords(self):
        logging.info('Reading password calculator test set...')
        filterer = Filterer(self.config, True)
        pwd_lister = PwdList(self.config.password_test_fname)
        pwd_input = list(pwd_lister.as_list())
        pwds = list(filterer.filter(pwd_input))
        filterer.finish(save_stats=False, save_freqs=False)
        return pwds

    def do_calculate_probs_from_file(self):
        if self.config.probability_steps:
            return self._use_probability_steps()

        pwds = self.read_test_passwords()
        logging.info('Calculating test set probabilities')
        if self.config.sequence_model == Sequence.MANY_TO_MANY:
            return ManyToManyProbabilityCalculator(self).calc_probabilities(pwds)

        if self.config.sequence_model == Sequence.MANY_TO_ONE:
            return ProbabilityCalculator(self).calc_probabilities(pwds)

        raise ValueError(
            'unknown sequence_model: %s' % self.config.sequence_model)

    def _use_probability_steps(self):
        return [(str(i), i) for i in self.config.probability_steps]

    def calculate_probs_from_file(self):
        if self._calc_prob_cache is None:
            logging.info('Calculating pwd prob from file for the first time')
            self._calc_prob_cache = list(self.do_calculate_probs_from_file())

        return self._calc_prob_cache

    def _should_make_guesses_rare_char_optimizer(self):
        return ((self.config.uppercase_character_optimization or
                 self.config.rare_character_optimization) and
                self.config.rare_character_optimization_guessing)

    def make_serializer(self, method=None, make_rare=None):
        if method is None:
            method = self.config.guess_serialization_method
        if make_rare is None:
            make_rare = self.should_make_guesses_rare_char_optimizer
        serializer_factory = serializer_type_list[method]
        if method == 'calculator':
            answer = serializer_factory(
                self.ostream, self.calculate_probs_from_file())
        elif method == 'delamico_random_walk':
            answer = serializer_factory(
                self.ostream, self.calculate_probs_from_file(), self.config)
        else:
            answer = serializer_factory(self.ostream)
        if self.config.enforced_policy != 'basic':
            answer = PasswordPolicyEnforcingSerializer(
                BasePasswordPolicy.fromConfig(self.config), answer)
        if make_rare:
            logging.info('Using template converting password serializer')
            answer = PasswordTemplateSerializer(self.config, answer)

        return answer

    def relevel_prediction(self, preds, astring):
        if isinstance(astring, tuple):
            astring_joined_len = sum(map(len, astring))
        else:
            astring_joined_len = 0
        if not self.filterer.pwd_is_valid(astring, quick=True):
            preds[self.ctable.get_char_index(PASSWORD_END)] = 0
        elif len(astring) == self.max_len or (
                isinstance(astring, tuple) and
                astring_joined_len == self.max_len):
            multiply = np.zeros(len(preds))
            pwd_end_idx = self.ctable.get_char_index(PASSWORD_END)
            multiply[pwd_end_idx] = 1
            preds[pwd_end_idx] = 1
            preds = np.multiply(preds, multiply, preds)

        sum_per = sum(preds)
        for i, v in enumerate(preds):
            preds[i] = v / sum_per

    def relevel_prediction_many(self, pred_list, str_list):
        if (self.filterer.pwd_is_valid(str_list[0], quick=True) and
            len(str_list[0]) != self.max_len):
            return
        for i, pred_item in enumerate(pred_list):
            self.relevel_prediction(pred_item[0], str_list[i])

    def conditional_probs(self, astring):
        return self.conditional_probs_many([astring])[0][0].copy()

    def conditional_probs_many(self, astring_list):
        if self.config.sequence_model == Sequence.MANY_TO_MANY:
            predict_strings, astring_list = self.ctable.encode_many_chunks(astring_list,
                                                                           self.config.max_len)
            answer = self.model.predict(predict_strings,
                                    verbose=0,
                                    batch_size=self.chunk_size_guesser)
        else:
            answer = self.model.predict(self.ctable.encode_many(astring_list),
                                    verbose=0,
                                    batch_size=self.chunk_size_guesser)
        # pylint: disable=no-member
        #
        # numpy does have the float64 datatype
        answer = np.array(answer, dtype=np.float64)
        # Versions of the Keras library after about 0.2.0 return a different
        # shape than the library before 0.3.1
        if len(answer.shape) == 2:
            answer = np.expand_dims(answer, axis=1)
        if self.config.sequence_model == Sequence.MANY_TO_MANY:
            assert answer.shape == (len(predict_strings),
                                    self.config.context_length, self.ctable.vocab_size)
        else:
            assert answer.shape == (len(astring_list), 1, self.ctable.vocab_size)
        if self.relevel_not_matching_passwords:
            self.relevel_prediction_many(answer, astring_list)
        return answer

    def next_nodes(self, astring, prob, prediction):
        total_preds = prediction * prob
        if len(astring) + 1 > self.max_len:
            prob_end = total_preds[self.pwd_end_idx]
            if prob_end >= self.lower_probability_threshold:
                self.output_serializer.serialize(astring, prob_end)
                self.generated += 1
            return []
        indexes = np.arange(len(total_preds))
        above_cutoff = total_preds >= self.lower_probability_threshold
        above_indices = indexes[above_cutoff]
        probs_above = total_preds[above_cutoff]
        answer = []
        for i, chain_prob in enumerate(probs_above):
            char = self.chars_list[above_indices[i]]
            if char == PASSWORD_END:
                self.output_serializer.serialize(astring, chain_prob)
                self.generated += 1
            else:
                chain_pass = astring + char
                answer.append((chain_pass, chain_prob))
        return answer

    def batch_prob(self, prefixes):
        if len(prefixes) > self.max_gpu_prediction_size:
            if self.config.sequence_model == Sequence.MANY_TO_MANY:
                answer = np.zeros(
                    (0, self.config.context_length, len(self.chars_list)))
            else:
                answer = np.zeros((0, 1, len(self.chars_list)))
            for chunk_num in range(
                    math.ceil(len(prefixes) / self.max_gpu_prediction_size)):
                answer = np.concatenate(
                    (answer, self.conditional_probs_many(prefixes[
                        self.max_gpu_prediction_size * chunk_num:
                        self.max_gpu_prediction_size * (chunk_num + 1)])), 0)
            return answer
        return self.conditional_probs_many(prefixes)

    def _extract_pwd_from_node(self, node_list):
        return map(lambda x: x[0], node_list)

    def super_node_recur(self, node_list):
        if len(node_list) == 0:
            return
        pwds_list = list(self._extract_pwd_from_node(node_list))
        predictions = self.batch_prob(pwds_list)
        node_batch = []
        for i, cur_node in enumerate(node_list):
            astring, prob = cur_node
            for next_node in self.next_nodes(astring, prob, predictions[i][0]):
                node_batch.append(next_node)
                if len(node_batch) == self.chunk_size_guesser:
                    self.super_node_recur(node_batch)
                    node_batch = []
        if len(node_batch) > 0:
            self.super_node_recur(node_batch)
            node_batch = []

    def _recur(self, astring='', prob=1):
        self.super_node_recur([(astring, prob)])

    def starting_node(self, default_value):
        return default_value

    def guess(self, astring='', prob=1):
        self._recur(self.starting_node(astring), prob)

    def complete_guessing(self, start='', start_prob=1):
        logging.info('Enumerating guesses starting at %s, %s...',
                     start, start_prob)
        self.guess(start, start_prob)
        self.output_serializer.finish()
        logging.info('Generated %s guesses', self.generated)
        return self.generated

    def _calculate_probs_from_file_sorted(self):
        return sorted(
            self.calculate_probs_from_file(), key=lambda x: x[1])

    def calculate_probs(self):
        logging.info('Calculating probabilities only')
        writer = csv.writer(self.ostream, delimiter='\t', quotechar=None)
        for pwd, prob in self._calculate_probs_from_file_sorted():
            writer.writerow([pwd, prob])
        self.ostream.flush()
        self.ostream.close()

    @staticmethod
    def read_guess_number_cache_from_file(fname):
        if fname is None:
            raise ValueError(
                'Must specify previous probability to guess number file')

        logging.info('Reading guess number cache from %s', fname)
        with open(fname, 'r') as cache:
            return Guesser.read_guess_number_cache(cache)

    @staticmethod
    def read_guess_number_cache(cache):
        logging.info('Reading guess number cache')
        prob_table = []
        for row in csv.reader(cache, delimiter='\t', quotechar=None):
            prob, guess_number = float(row[1]), float(row[2])
            prob_table.append((prob, guess_number))

        prob_table.sort(key=lambda x: x[0])
        probs = [prob for prob, _ in prob_table]
        guess_numbers = [gn for _, gn in prob_table]
        logging.info('Done reading guess number cache')
        return probs, guess_numbers

    @staticmethod
    def calculate_guess_numbers_from_cache_helper(guess_numbers, test_probs):
        probs, guess_number_list = guess_numbers
        assert len(probs) == len(guess_number_list)
        assert len(probs) > 0
        for pwd, prob in test_probs:
            gn = Guesser._calculate_guess_number_given_cache_idx(
                prob, probs, guess_number_list)
            yield pwd, prob, gn

    @staticmethod
    def _calculate_guess_number_given_cache_idx(prob, probs, guess_numbers):
        idx = bisect.bisect_left(probs, prob)
        if idx == len(guess_numbers):
            return guess_numbers[-1] if prob == probs[-1] else 0

        return guess_numbers[idx]

    def calculate_guess_numbers_from_cache(self):
        logging.info('Calculating guess numbers using cache')
        writer = csv.writer(self.ostream, delimiter='\t', quotechar=None)
        answer = self.calculate_guess_numbers_from_cache_helper(
            self.read_guess_number_cache_from_file(
                self.config.previous_probability_mapping_file),
            self._calculate_probs_from_file_sorted())
        for pwd, prob, guess_number in answer:
            writer.writerow([pwd, prob, guess_number])

        self.ostream.flush()
        self.ostream.close()
        logging.info('Done calculating guess numbers using cache')

    def create_guess_number_cache_predictor(self):
        probs, guess_numbers = self.read_guess_number_cache_from_file(
            self.config.previous_probability_mapping_file)
        prob_calculator = ProbabilityCalculator(
            self,
            prefixes=False,
            cache_size=self.config.probability_calculator_cache_size)

        def _predictor(pwd):
            prob_as_list = list(prob_calculator.calc_probabilities([(pwd, 1)]))
            assert len(prob_as_list) == 1
            pwd_copy, prob = prob_as_list[0]
            assert pwd == pwd_copy
            return Guesser._calculate_guess_number_given_cache_idx(
                prob, probs, guess_numbers)

        return _predictor


class RandomWalkSerializer(GuessSerializer):
    def serialize(self, password, prob):
        self.total_guessed += 1

    def get_stats(self):
        raise NotImplementedError()

class RandomWalkGuesser(Guesser):
    def __init__(self, *args):
        super().__init__(*args)
        if self.should_make_guesses_rare_char_optimizer:
            self._chars_list = self.char_bag
        else:
            self._chars_list = self.chars_list
        self.pwd_end_idx = self._chars_list.index(PASSWORD_END)
        self.enforced_policy = self.config.enforced_policy != 'basic'
        if self.enforced_policy:
            self.policy = BasePasswordPolicy.fromConfig(self.config)
        self.expander = self.output_serializer

         # pylint: disable=I1101
         # generator has this field
        self.next_node_fn = generator.next_nodes_random_walk
        self.estimates = []

    def next_nodes_random_walk_tuple(self, astring, prob, prediction):
        if len(astring) > 0 and astring[-1] == PASSWORD_END:
            return []
        astring_len = sum(map(len, astring))
        if self.should_make_guesses_rare_char_optimizer:
            conditional_predictions = (
                self.expander.expand_conditional_probs(
                    prediction, astring))
        else:
            conditional_predictions = prediction
        total_preds = conditional_predictions * prob
        if astring_len + 1 > self.max_len:
            if total_preds[self.pwd_end_idx] > self.lower_probability_threshold:
                return [(astring + (PASSWORD_END,),
                         total_preds[self.pwd_end_idx],
                         conditional_predictions[self.pwd_end_idx])]
        indexes = np.arange(len(total_preds))
        above_cutoff = total_preds > self.lower_probability_threshold
        above_indices = indexes[above_cutoff]
        probs_above = total_preds[above_cutoff]
        answer = [0] * len(probs_above)
        for i, prob_above in enumerate(probs_above):
            index = above_indices[i]
            answer[i] = (astring + (self._chars_list[index],), prob_above,
                         conditional_predictions[index])
        return answer

    def spinoff_node(self, node):
        _, _, _, cost_fn = node
        self.estimates.append(cost_fn)

    def add_to_next_node(self, cur_node, next_node):
        _, _, d_accum, cost_accum = cur_node
        pwd, prob, pred = next_node
        d_accum_next = d_accum / pred
        return (pwd, prob, d_accum_next, (
            cost_accum + d_accum_next * self.cost_of_node(pwd)))

    def super_node_recur(self, node_list):
        real_node_list = []
        for node in node_list:
            pwd = node[0]
            if len(pwd) <= self.max_len:
                real_node_list.append(node)
            elif len(pwd) > self.max_len and pwd[-1] == PASSWORD_END:
                self.spinoff_node(node)
        if len(real_node_list) == 0:
            return
        pwd_list = list(self._extract_pwd_from_node(real_node_list))
        predictions = self.batch_prob(pwd_list)
        next_nodes = []
        for i, cur_node in enumerate(real_node_list):
            astring, prob = cur_node[0], cur_node[1]
            if self.config.sequence_model == Sequence.MANY_TO_MANY:
                poss_next = self.next_node_fn(
                    self, astring, prob, predictions[i][len(astring)-1])
            else:
                poss_next = self.next_node_fn(
                    self, astring, prob, predictions[i][0])
            if len(poss_next) == 0:
                self.spinoff_node(cur_node)
                continue
            next_nodes.append(self.add_to_next_node(
                cur_node, self.choose_next_node(poss_next)))
        if len(next_nodes) != 0:
            self.super_node_recur(next_nodes)

    def choose_next_node(self, node_list):
        total = sum(map(lambda x: x[2], node_list))
        r = random.uniform(0, total)
        upto = 0
        for pwd, prob, cond_prob in node_list:
            if upto + cond_prob > r:
                return pwd, prob, (cond_prob / total)
            upto += cond_prob

        raise Exception("unreachable")

    def cost_of_node(self, pwd):
        if len(pwd) == 0 or pwd[-1] != PASSWORD_END:
            return 0
        if self.enforced_policy:
            if self.policy.pwd_complies(pwd):
                return 1

            return 0

        return 1

    def seed_data(self):
        for _ in range(self.config.random_walk_seed_num):
            if self.config.sequence_model == Sequence.MANY_TO_MANY:
                yield self.starting_node('\t'), 1, 1, 0
            else:
                yield self.starting_node(''), 1, 1, 0

    def calc_error(self):
        return self.config.random_walk_confidence_bound_z_value * (
            np.std(self.estimates) / math.sqrt(len(self.estimates)))

    def random_walk(self, probs):
        for prob_node in probs:
            pwd, prob = prob_node
            logging.info('Calculating guess number for %s at %s', pwd, prob)
            self.lower_probability_threshold = prob
            self.estimates = []
            error = -1
            num = 0
            while True:
                self.super_node_recur(list(self.seed_data()))
                num += 1
                if len(self.estimates) == 0:
                    logging.error(("Number of passwords guessed is 0 for all "
                                   "branches! I don't know what this means"
                                   "but its probably a bug. "))
                if self.calc_error() < (np.average(self.estimates) * .01 * (
                        self.config.random_walk_confidence_percent)) or (
                            num > self.config.random_walk_upper_bound):
                    break
            cost = np.average(self.estimates)
            stdev = np.std(self.estimates)
            error = self.calc_error()
            self.ostream.write('%s\t%s\t%s\t%s\t%s\t%s\n' % (
                pwd, prob, cost, stdev, len(self.estimates), error))
            self.ostream.flush()

    def guess(self, astring='', prob=1):
        pwds_probs = list(self.calculate_probs_from_file())
        logging.debug('Beginning probabilities: %s', json.dumps(
            pwds_probs, indent=4))
        self.random_walk(pwds_probs)

class RandomWalkDelAmico(RandomWalkGuesser):
    def spinoff_node(self, node):
        pwd, prob = node
        self.output_serializer.serialize(pwd, prob)

    def make_serializer(self, method=None, make_rare=None):
        self.config.lower_probability_threshold = 0
        if make_rare is None:
            make_rare = False

        return super().make_serializer(method=method, make_rare=make_rare)

    def add_to_next_node(self, cur_node, next_node):
        return next_node[0], next_node[1]

    def keep_going(self):
        for item in self.output_serializer.get_stats():
            if item[-1] > (item[2] * .01 * (
                    self.config.random_walk_confidence_percent)):
                return True
        return False

    def setup(self):
        if self.should_make_guesses_rare_char_optimizer:
            self.expander = PasswordTemplateSerializer(self.config)
        self.lower_probability_threshold = 0

    def random_walk(self, probs):
        self.setup()
        num = 0
        self.super_node_recur(list(self.seed_data()))
        while self.keep_going() and num < self.config.random_walk_upper_bound:
            self.super_node_recur(list(self.seed_data()))
            num += 1

class RandomGenerator(RandomWalkDelAmico):
    def spinoff_node(self, node):
        pwd, prob = node[0], node[1]
        self.output_serializer.serialize(pwd.rstrip('\n'), prob)

    def make_serializer(self, method=None, make_rare=None):
        return super().make_serializer(method='human', make_rare=make_rare)

    def guess(self, astring='', prob=1):
        self.setup()
        for _ in range(self.config.random_walk_upper_bound):
            self.super_node_recur(list(self.seed_data()))

class DelAmicoCalculator(GuessSerializer):
    def __init__(self, ostream, pwd_list, config):
        super().__init__(ostream)
        self.pwds, self.probs = zip(*sorted(pwd_list, key=lambda x: x[1]))
        self.pwds = list(self.pwds)
        self.config = config
        for i, pwd in enumerate(self.pwds):
            if isinstance(pwd, tuple):
                self.pwds[i] = ''.join(pwd)
        self.guess_numbers = []
        for _ in range(len(self.pwds)):
            self.guess_numbers.append([])
        self.random_walk_confidence_bound_z_value = (
            config.random_walk_confidence_bound_z_value)

    def serialize(self, password, prob):
        self.total_guessed += 1
        if prob == 0:
            return
        idx = bisect.bisect_left(self.probs, prob) - 1
        if idx >= 0:
            self.guess_numbers[idx].append(prob)

    def get_stats(self):
        out_guess_numbers = [0] * len(self.guess_numbers)
        out_variance = [0] * len(self.guess_numbers)
        out_stdev = [0] * len(self.guess_numbers)
        out_error = [0] * len(self.guess_numbers)
        num_guess = self.get_total_guessed()
        guess_nums = list(map(lambda items: list(
            map(lambda x: 1/x, items)), self.guess_numbers))
        for i in range(len(self.guess_numbers)):
            out_guess_numbers[i] = sum(guess_nums[i]) / num_guess
        for j in range(len(out_guess_numbers) - 1, 0, -1):
            out_guess_numbers[j - 1] += out_guess_numbers[j]
        for i in range(len(self.guess_numbers)):
            out_variance[i] = (sum(map(
                lambda e: (e - out_guess_numbers[i])**2, guess_nums[i])) /
                               num_guess)
        for j in range(len(out_guess_numbers) - 1, 0, -1):
            out_variance[j - 1] += out_variance[j]
        out_stdev = list(map(math.sqrt, out_variance))
        for i in range(len(self.guess_numbers)):
            out_error[i] = self.random_walk_confidence_bound_z_value * (
                out_stdev[i] / math.sqrt(num_guess))
        for i in range(len(self.pwds), 0, -1):
            idx = i - 1
            if self.config.sequence_model == Sequence.MANY_TO_MANY:
                yield [
                    self.pwds[idx].lstrip('\t'), self.probs[idx], out_guess_numbers[idx],
                    out_stdev[idx], num_guess, out_error[idx]]
            else:
                yield [
                    self.pwds[idx], self.probs[idx], out_guess_numbers[idx],
                    out_stdev[idx], num_guess, out_error[idx]]

    def finish(self):
        logging.info('Guessed %s passwords', self.get_total_guessed())
        writer = csv.writer(self.ostream, delimiter='\t', quotechar=None)
        for item in self.get_stats():
            writer.writerow(item)
        self.ostream.flush()
        self.ostream.close()

class GuesserBuilderError(Exception):
    pass

class GuesserBuilder():
    special_class_builder_map = {
        'random_walk' : RandomWalkGuesser,
        'delamico_random_walk' : RandomWalkDelAmico,
        'generate_random' : RandomGenerator,
    }

    other_class_builders = {}

    def __init__(self, config):
        self.config = config
        self.model = None
        self.serializer = None
        self.ostream = None
        self.ofile_path = None

    def add_model(self, model):
        self.model = model
        return self

    def add_serializer(self, serializer):
        self.serializer = serializer
        return self

    def add_stream(self, ostream):
        self.ostream = ostream
        return self

    def add_file(self, ofname):
        self.add_stream(open(ofname, 'w'))
        self.ofile_path = ofname
        return self

    def add_temp_file(self):
        intm_dir = self.config.guesser_intermediate_directory
        handle, path = tempfile.mkstemp(dir=intm_dir)
        self.ofile_path = path
        return self.add_stream(os.fdopen(handle, 'w'))

    def build(self):
        model_or_serializer = self.model
        if self.serializer is not None and self.model is None:
            model_or_serializer = self.serializer.load_model()

        if model_or_serializer is None:
            raise GuesserBuilderError('Cannot build without model')

        assert self.config is not None
        class_builder = Guesser
        guess_serialization_method = self.config.guess_serialization_method
        if guess_serialization_method in self.special_class_builder_map:
            class_builder = self.special_class_builder_map[
                guess_serialization_method]
        if self.config.guesser_class in self.other_class_builders:
            class_builder = self.other_class_builders[self.config.guesser_class]

        return class_builder(model_or_serializer, self.config, self.ostream)

log_level_map = {
    'info' : logging.INFO,
    'warning'  : logging.WARNING,
    'debug' : logging.DEBUG,
    'error' : logging.ERROR
}

serializer_type_list = {
    'human' : GuessSerializer,
    'calculator' : GuessNumberGenerator,
    'random_walk' : RandomWalkSerializer,
    'delamico_random_walk' : DelAmicoCalculator,
    'generate_random' : DelAmicoCalculator
}

def get_version_string():
    p = subp.Popen(['git', 'log', '--pretty=format:%H', '-n', '1'],
                   cwd=os.path.dirname(os.path.realpath(__file__)),
                   stdin=subp.PIPE, stdout=subp.PIPE, stderr=subp.PIPE)
    output, _ = p.communicate()
    return output.decode('utf-8').strip('\n')

def init_logging(args):
    def except_hook(exctype, value, tb):
        logging.critical('Uncaught exception', exc_info=(exctype, value, tb))
        sys.stderr.write('Uncaught exception!\n %s\n' % (value))
    sys.excepthook = except_hook
    sys.setcheckinterval = 1000
    log_format = '%(asctime)-15s %(levelname)s: %(message)s'
    log_level = log_level_map[args['log_level']]
    if args['log_file']:
        logging.basicConfig(filename=args['log_file'],
                            level=log_level, format=log_format)
    else:
        logging.basicConfig(level=log_level, format=log_format)
    logging.info('Beginning...')
    logging.info('Arguments: %s', json.dumps(args, indent=4))
    logging.info('Version: %s', get_version_string())

def preprocessing(args, config):
    resetable = ResetablePwdList(args['pwd_file'], args['pwd_format'], config)
    # must be called before creating the preprocessor because it
    # initializes statistics needed for some preprocessors
    if 'no_initialize' not in args:
        resetable.initialize()
    else:
        resetable.initialize(*args['no_initialize'])
    if args['stats_only']:
        logging.info('Only getting stats. Quitting...')
        return None
    preprocessor = BasePreprocessor.fromConfig(config)
    preprocessor.begin_resetable(resetable)
    if args['pre_processing_only']:
        logging.info('Only performing pre-processing. ')
        if config.compute_stats:
            preprocessor.stats()
        return None
    return preprocessor

def prepare_secondary_training(config):
    logging.info('Secondary training')
    fake_args = config.secondary_train_sets
    fake_args['stats_only'] = False
    fake_args['pre_processing_only'] = False
    fake_args['no_initialize'] = [
        False, config.secondary_training_save_freqs]
    return preprocessing(fake_args, config)

def train(args, config):
    preprocessor = preprocessing(args, config)
    if preprocessor is None:
        return
    if config.sequence_model == Sequence.MANY_TO_MANY:
        trainer = ManyToManyTrainer(preprocessor, config, args['multi_gpu'])
    else:
        trainer = Trainer(preprocessor, config, args['multi_gpu'])

    serializer = ModelSerializer(args['arch_file'], args['weight_file'],
                                 config.save_model_versioned)
    if args['retrain']:
        logging.info('Retraining model...')
        trainer.model = serializer.load_model()
        trainer.init_layers()
    if not args['train_secondary_only']:
        logging.info('Training model with primary data')
        trainer.train(serializer)
    else:
        logging.info('Not training model with primary data')
    if config.secondary_training:
        trainer.retrain_classification(
            prepare_secondary_training(config), serializer)
    if args['enumerate_ofile']:
        raise ValueError('Can either train or guess')

def guess(args, config):
    logging.info('Loading model...')
    if args['arch_file'] is None or args['weight_file'] is None:
        logging.error('Architecture file or weight file not found. Quiting...')
        sys.exit(1)
    if config.guessing_secondary_training:
        prepare_secondary_training(config)
    guesser = (GuesserBuilder(config)
               .add_serializer(
                   ModelSerializer(
                       archfile=args['arch_file'],
                       weightfile=args['weight_file'],
                       multi_gpu=args['multi_gpu']))
               .add_file(args['enumerate_ofile'])
               .build())
    if args['calc_probability_only']:
        guesser.calculate_probs()
    elif args["calc_guess_number_from_cache"]:
        guesser.calculate_guess_numbers_from_cache()
    else:
        guesser.complete_guessing()

def read_config_args(args):
    config_args = read_config_file(args['config_args'])

    arg_ret = args.copy()
    arg_ret.update(config_args['args'])
    if 'profile' in config_args['args']:
        logging.warning(('Profile argument must be given at command line. '
                         'Proceeding without profiling. '))
    config = ModelDefaults(config_args['config'])
    return config, arg_ret

def main(args):
    if args['version']:
        sys.stdout.write(get_version_string() + '\n')
        sys.exit(0)
    if args['config_args']:
        config, args = read_config_args(args)
    else:
        config = ModelDefaults.fromFile(args['config'])
    config.override_from_commandline(args['config_cmdline'])
    config.sequence_model_updates()
    if args['args']:
        with open(args['args'], 'r') as argfile:
            args = json.load(argfile)
    init_logging(args)
    try:
        config.validate()
    except AssertionError as e:
        logging.critical('Configuration not valid %s', str(e))
        raise
    logging.info('Configuration: %s', json.dumps(config.as_dict(), indent=4))

    if args['pwd_file']:
        train(args, config)
    elif args['enumerate_ofile']:
        guess(args, config)
    if not args['pwd_file'] and not args['enumerate_ofile']:
        logging.error('Nothing to do! Use --pwd-file or --enumerate-ofile. ')
        sys.exit(1)
    logging.info('Done!')

def make_parser():
    parser = argparse.ArgumentParser(description=(
        """Neural Network with passwords. This program uses a neural network to
        guess passwords. This happens in two phases, training and
        enumeration. Either --pwd-file or --enumerate-ofile are required.
        --pwd-file will give a password file as training data.
        --enumerate-ofile will guess passwords based on an existing model.
        Version """ +
        get_version_string()))
    parser.add_argument('--pwd-file', help=('Input file name. '), nargs='+')
    parser.add_argument('--arch-file',
                        help='Output file for the model architecture. ')
    parser.add_argument('--weight-file',
                        help='Output file for the weights of the model. ')
    parser.add_argument('--pwd-format', default='list', nargs='+',
                        choices=['trie', 'tsv', 'list', 'im_trie'],
                        help=('Format of pwd-file input. "list" format is one'
                              'password per line. "tsv" format is tab '
                              'separated values: first column is the '
                              'password, second is the frequency in floating'
                              ' hex. "trie" is a custom binary format created'
                              ' by another step of this tool. '))
    parser.add_argument('--enumerate-ofile',
                        help='Enumerate guesses output file')
    parser.add_argument('--retrain', action='store_true',
                        help=('Instead of training a new model, begin '
                              'training the model in the weight-file and '
                              'arch-file arguments. '))
    parser.add_argument('--config', help='Config file in json. ')
    parser.add_argument('--args', help='Argument file in json. ')
    parser.add_argument('--profile',
                        help='Profile execution and save to the given file. ')
    parser.add_argument('--log-file')
    parser.add_argument('--log-level', default='info',
                        choices=['debug', 'info', 'warning', 'error'])
    parser.add_argument('--version', action='store_true',
                        help='Print version number and exit')
    parser.add_argument('--pre-processing-only', action='store_true',
                        help='Only perform the preprocessing step. ')
    parser.add_argument('--stats-only', action='store_true',
                        help=('Quit after reading in passwords and saving '
                              'stats. '))
    parser.add_argument('--config-args',
                        help='File with both configuration and arguments. ')
    parser.add_argument('--calc-probability-only', action='store_true',
                        help='Only output password probabilities')
    parser.add_argument(
        '--calc-guess-number-from-cache', action='store_true',
        help=('Estimate guess numbers from previous results. This argument '
              'expects a path to write the results of the output file. '
              'The output is a TSV of (password, probability, guess number). '
              'Must provide the previous_probability_mapping_file config '
              'option. It should be a path to a TSV with columns (password, '
              'probability, guess number). This file may be created from one '
              'of the guessing methods, particularly with the '
              'probability_steps configuration option. '))
    parser.add_argument('--train-secondary-only', action='store_true',
                        help='Only train on secondary data. ')
    parser.add_argument('--multi-gpu', default=1, type=int,
                        help="The number of GPUs to use to train in parallel")
    parser.add_argument('--config-cmdline', default='',
                        help=('Extra configuration values. Should be a list of '
                              'key1=value1;key2=value2 elements. '))
    return parser

def main_entry_point():
    args = vars(make_parser().parse_args())
    main_bundle = lambda: main(args)
    if args['profile'] is not None:
        cProfile.run('main_bundle()', filename=args['profile'])
    else:
        main_bundle()

if __name__ == '__main__':
    main_entry_point()
