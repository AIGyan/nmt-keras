# -*- coding: utf-8 -*-
from __future__ import print_function
from six import iteritems
import argparse
import ast
import copy
import sys
import time
from timeit import default_timer as timer

from config import load_parameters
from config_online import load_parameters as load_parameters_online
from data_engine.prepare_data import build_dataset, update_dataset_from_file
from keras_wrapper.cnn_model import loadModel, saveModel, updateModel
from keras_wrapper.dataset import loadDataset, saveDataset
from keras_wrapper.extra.callbacks import *
from keras_wrapper.model_ensemble import BeamSearchEnsemble
from keras_wrapper.online_trainer import OnlineTrainer
from model_zoo import TranslationModel
from online_models import build_online_models
from utils.utils import *

logging.basicConfig(level=logging.DEBUG, format='[%(asctime)s] %(message)s', datefmt='%d/%m/%Y %H:%M:%S')


def parse_args():
    parser = argparse.ArgumentParser("Train or sample NMT models")
    parser.add_argument("-c", "--config", required=False, help="Config pkl for loading the model configuration. "
                                                               "If not specified, hyperparameters "
                                                               "are read from config.py")
    parser.add_argument("-o", "--online",
                        action='store_true', default=False, required=False, help="Online training mode. ")
    parser.add_argument("-s", "--splits", nargs='+', required=False, default=['val'],
                        help="Splits to train on. Should be already included into the dataset object.")
    parser.add_argument("-ds", "--dataset", required=False, help="Dataset instance with data")
    parser.add_argument("-m", "--models", nargs='*', required=False, help="Models to load", default="")
    parser.add_argument("-src", "--source", help="File of source hypothesis", required=False)
    parser.add_argument("-trg", "--references", help="Reference sentence", required=False)
    parser.add_argument("-hyp", "--hypotheses", required=False, help="Store hypothesis to this file")
    parser.add_argument("-v", "--verbose", required=False, default=0, type=int, help="Verbosity level")
    parser.add_argument("-ch", "--changes", nargs="*", help="Changes to config, following the syntax Key=Value",
                        default="")

    return parser.parse_args()


def train_model(params, load_dataset=None):
    """
    Training function. Sets the training parameters from params. Build or loads the model and launches the training.
    :param params: Dictionary of network hyperparameters.
    :param load_dataset: Load dataset from file or build it from the parameters.
    :return: None
    """
    check_params(params)

    if params['RELOAD'] > 0:
        logging.info('Resuming training.')
        # Load data
        if load_dataset is None:
            if params['REBUILD_DATASET']:
                logging.info('Rebuilding dataset.')
                dataset = build_dataset(params)
            else:
                logging.info('Updating dataset.')
                dataset = loadDataset(params['DATASET_STORE_PATH'] + '/Dataset_' + params['DATASET_NAME']
                                      + '_' + params['SRC_LAN'] + params['TRG_LAN'] + '.pkl')
                params['EPOCH_OFFSET'] = params['RELOAD'] if params['RELOAD_EPOCH'] else \
                    int(params['RELOAD'] * params['BATCH_SIZE'] / dataset.len_train)
                for split, filename in iteritems(params['TEXT_FILES']):
                    dataset = update_dataset_from_file(dataset,
                                                       params['DATA_ROOT_PATH'] + '/' + filename + params['SRC_LAN'],
                                                       params,
                                                       splits=list([split]),
                                                       output_text_filename=params['DATA_ROOT_PATH'] + '/' +
                                                                            filename + params['TRG_LAN'],
                                                       remove_outputs=False,
                                                       compute_state_below=True,
                                                       recompute_references=True)
                    dataset.name = params['DATASET_NAME'] + '_' + params['SRC_LAN'] + params['TRG_LAN']
                saveDataset(dataset, params['DATASET_STORE_PATH'])

        else:
            logging.info('Reloading and using dataset.')
            dataset = loadDataset(load_dataset)
    else:
        # Load data
        if load_dataset is None:
            dataset = build_dataset(params)
        else:
            dataset = loadDataset(load_dataset)

    params['INPUT_VOCABULARY_SIZE'] = dataset.vocabulary_len[params['INPUTS_IDS_DATASET'][0]]
    params['OUTPUT_VOCABULARY_SIZE'] = dataset.vocabulary_len[params['OUTPUTS_IDS_DATASET'][0]]

    # Build model
    set_optimizer = True if params['RELOAD'] == 0 else False
    clear_dirs = True if params['RELOAD'] == 0 else False

    # build new model
    nmt_model = TranslationModel(params,
                                 model_type=params['MODEL_TYPE'],
                                 verbose=params['VERBOSE'],
                                 model_name=params['MODEL_NAME'],
                                 vocabularies=dataset.vocabulary,
                                 store_path=params['STORE_PATH'],
                                 set_optimizer=set_optimizer,
                                 clear_dirs=clear_dirs)

    # Define the inputs and outputs mapping from our Dataset instance to our model
    inputMapping = dict()
    for i, id_in in enumerate(params['INPUTS_IDS_DATASET']):
        pos_source = dataset.ids_inputs.index(id_in)
        id_dest = nmt_model.ids_inputs[i]
        inputMapping[id_dest] = pos_source
    nmt_model.setInputsMapping(inputMapping)

    outputMapping = dict()
    for i, id_out in enumerate(params['OUTPUTS_IDS_DATASET']):
        pos_target = dataset.ids_outputs.index(id_out)
        id_dest = nmt_model.ids_outputs[i]
        outputMapping[id_dest] = pos_target
    nmt_model.setOutputsMapping(outputMapping)

    if params['RELOAD'] > 0:
        nmt_model = updateModel(nmt_model, params['STORE_PATH'], params['RELOAD'], reload_epoch=params['RELOAD_EPOCH'])
        nmt_model.setParams(params)
        nmt_model.setOptimizer()
        if params.get('EPOCH_OFFSET') is None:
            params['EPOCH_OFFSET'] = params['RELOAD'] if params['RELOAD_EPOCH'] else \
                int(params['RELOAD'] * params['BATCH_SIZE'] / dataset.len_train)

    # Store configuration as pkl
    dict2pkl(params, params['STORE_PATH'] + '/config')

    # Callbacks
    callbacks = buildCallbacks(params, nmt_model, dataset)

    # Training
    total_start_time = timer()

    logging.debug('Starting training!')
    training_params = {'n_epochs': params['MAX_EPOCH'],
                       'batch_size': params['BATCH_SIZE'],
                       'homogeneous_batches': params['HOMOGENEOUS_BATCHES'],
                       'maxlen': params['MAX_OUTPUT_TEXT_LEN'],
                       'joint_batches': params['JOINT_BATCHES'],
                       'lr_decay': params.get('LR_DECAY', None),  # LR decay parameters
                       'reduce_each_epochs': params.get('LR_REDUCE_EACH_EPOCHS', True),
                       'start_reduction_on_epoch': params.get('LR_START_REDUCTION_ON_EPOCH', 0),
                       'lr_gamma': params.get('LR_GAMMA', 0.9),
                       'lr_reducer_type': params.get('LR_REDUCER_TYPE', 'linear'),
                       'lr_reducer_exp_base': params.get('LR_REDUCER_EXP_BASE', 0),
                       'lr_half_life': params.get('LR_HALF_LIFE', 50000),
                       'epochs_for_save': params['EPOCHS_FOR_SAVE'],
                       'verbose': params['VERBOSE'],
                       'eval_on_sets': params['EVAL_ON_SETS_KERAS'],
                       'n_parallel_loaders': params['PARALLEL_LOADERS'],
                       'extra_callbacks': callbacks,
                       'reload_epoch': params['RELOAD'],
                       'epoch_offset': params.get('EPOCH_OFFSET', 0),
                       'data_augmentation': params['DATA_AUGMENTATION'],
                       'patience': params.get('PATIENCE', 0),  # early stopping parameters
                       'metric_check': params.get('STOP_METRIC', None) if params.get('EARLY_STOP', False) else None,
                       'eval_on_epochs': params.get('EVAL_EACH_EPOCHS', True),
                       'each_n_epochs': params.get('EVAL_EACH', 1),
                       'start_eval_on_epoch': params.get('START_EVAL_ON_EPOCH', 0),
                       'tensorboard': params.get('TENSORBOARD', False),
                       'tensorboard_params': {'log_dir': params.get('LOG_DIR', 'tensorboard_logs'),
                                              'histogram_freq': params.get('HISTOGRAM_FREQ', 0),
                                              'batch_size': params.get('TENSORBOARD_BATCH_SIZE', params['BATCH_SIZE']),
                                              'write_graph': params.get('WRITE_GRAPH', True),
                                              'write_grads': params.get('WRITE_GRADS', False),
                                              'write_images': params.get('WRITE_IMAGES', False),
                                              'embeddings_freq': params.get('EMBEDDINGS_FREQ', 0),
                                              'embeddings_layer_names': params.get('EMBEDDINGS_LAYER_NAMES', None),
                                              'embeddings_metadata': params.get('EMBEDDINGS_METADATA', None),
                                              'label_word_embeddings_with_vocab': params.get('LABEL_WORD_EMBEDDINGS_WITH_VOCAB', False),
                                              'word_embeddings_labels': params.get('WORD_EMBEDDINGS_LABELS', None),
                                              }
                       }
    nmt_model.trainNet(dataset, training_params)

    total_end_time = timer()
    time_difference = total_end_time - total_start_time
    logging.info('In total is {0:.2f}s = {1:.2f}m'.format(time_difference, time_difference / 60.0))


def apply_NMT_model(params, load_dataset=None):
    """
    Sample from a previously trained model.

    :param params: Dictionary of network hyperparameters.
    :param load_dataset: Load dataset from file or build it from the parameters.
    :return: None
    """

    # Load data
    if load_dataset is None:
        dataset = build_dataset(params)
    else:
        dataset = loadDataset(load_dataset)
    params['INPUT_VOCABULARY_SIZE'] = dataset.vocabulary_len[params['INPUTS_IDS_DATASET'][0]]
    params['OUTPUT_VOCABULARY_SIZE'] = dataset.vocabulary_len[params['OUTPUTS_IDS_DATASET'][0]]

    # Load model
    nmt_model = loadModel(params['STORE_PATH'], params['RELOAD'], reload_epoch=params['RELOAD_EPOCH'])

    # Evaluate training
    extra_vars = {'language': params.get('TRG_LAN', 'en'),
                  'n_parallel_loaders': params['PARALLEL_LOADERS'],
                  'tokenize_f': eval('dataset.' + params['TOKENIZATION_METHOD']),
                  'detokenize_f': eval('dataset.' + params['DETOKENIZATION_METHOD']),
                  'apply_detokenization': params['APPLY_DETOKENIZATION'],
                  'tokenize_hypotheses': params['TOKENIZE_HYPOTHESES'],
                  'tokenize_references': params['TOKENIZE_REFERENCES'],
                  }

    input_text_id = params['INPUTS_IDS_DATASET'][0]
    vocab_x = dataset.vocabulary[input_text_id]['idx2words']
    vocab_y = dataset.vocabulary[params['OUTPUTS_IDS_DATASET'][0]]['idx2words']
    if params['BEAM_SEARCH']:
        extra_vars['beam_size'] = params.get('BEAM_SIZE', 6)
        extra_vars['state_below_index'] = params.get('BEAM_SEARCH_COND_INPUT', -1)
        extra_vars['maxlen'] = params.get('MAX_OUTPUT_TEXT_LEN_TEST', 30)
        extra_vars['optimized_search'] = params.get('OPTIMIZED_SEARCH', True)
        extra_vars['model_inputs'] = params['INPUTS_IDS_MODEL']
        extra_vars['model_outputs'] = params['OUTPUTS_IDS_MODEL']
        extra_vars['dataset_inputs'] = params['INPUTS_IDS_DATASET']
        extra_vars['dataset_outputs'] = params['OUTPUTS_IDS_DATASET']
        extra_vars['normalize_probs'] = params.get('NORMALIZE_SAMPLING', False)
        extra_vars['search_pruning'] = params.get('SEARCH_PRUNING', False)
        extra_vars['alpha_factor'] = params.get('ALPHA_FACTOR', 1.0)
        extra_vars['coverage_penalty'] = params.get('COVERAGE_PENALTY', False)
        extra_vars['length_penalty'] = params.get('LENGTH_PENALTY', False)
        extra_vars['length_norm_factor'] = params.get('LENGTH_NORM_FACTOR', 0.0)
        extra_vars['coverage_norm_factor'] = params.get('COVERAGE_NORM_FACTOR', 0.0)
        extra_vars['state_below_maxlen'] = -1 if params.get('PAD_ON_BATCH', True) \
            else params.get('MAX_OUTPUT_TEXT_LEN', 50)
        extra_vars['pos_unk'] = params['POS_UNK']
        extra_vars['output_max_length_depending_on_x'] = params.get('MAXLEN_GIVEN_X', True)
        extra_vars['output_max_length_depending_on_x_factor'] = params.get('MAXLEN_GIVEN_X_FACTOR', 3)
        extra_vars['output_min_length_depending_on_x'] = params.get('MINLEN_GIVEN_X', True)
        extra_vars['output_min_length_depending_on_x_factor'] = params.get('MINLEN_GIVEN_X_FACTOR', 2)
        extra_vars['attend_on_output'] = params.get('ATTEND_ON_OUTPUT', 'transformer' in params['MODEL_TYPE'].lower())

        if params['POS_UNK']:
            extra_vars['heuristic'] = params['HEURISTIC']
            if params['HEURISTIC'] > 0:
                extra_vars['mapping'] = dataset.mapping

    for s in params["EVAL_ON_SETS"]:
        extra_vars[s] = dict()
        extra_vars[s]['references'] = dataset.extra_variables[s][params['OUTPUTS_IDS_DATASET'][0]]
        callback_metric = PrintPerformanceMetricOnEpochEndOrEachNUpdates(nmt_model,
                                                                         dataset,
                                                                         gt_id=params['OUTPUTS_IDS_DATASET'][0],
                                                                         metric_name=params['METRICS'],
                                                                         set_name=params['EVAL_ON_SETS'],
                                                                         batch_size=params['BATCH_SIZE'],
                                                                         each_n_epochs=params['EVAL_EACH'],
                                                                         extra_vars=extra_vars,
                                                                         reload_epoch=params['RELOAD'],
                                                                         is_text=True,
                                                                         input_text_id=input_text_id,
                                                                         save_path=nmt_model.model_path,
                                                                         index2word_y=vocab_y,
                                                                         index2word_x=vocab_x,
                                                                         sampling_type=params['SAMPLING'],
                                                                         beam_search=params['BEAM_SEARCH'],
                                                                         start_eval_on_epoch=params['START_EVAL_ON_EPOCH'],
                                                                         write_samples=True,
                                                                         write_type=params['SAMPLING_SAVE_MODE'],
                                                                         eval_on_epochs=params['EVAL_EACH_EPOCHS'],
                                                                         save_each_evaluation=False,
                                                                         verbose=params['VERBOSE'])

        callback_metric.evaluate(params['RELOAD'], counter_name='epoch' if params['EVAL_EACH_EPOCHS'] else 'update')


def train_model_online(params, source_filename, target_filename, models_path=None, dataset=None, store_hypotheses=None,
                       verbose=0):
    """
    Training function. Sets the training parameters from params. Build or loads the model and launches the training.

    :param params: Dictionary of network hyperparameters.
    :param source_filename: Filename with source sentences
    :param target_filename: Filename with post-edited (reference) sentences
    :param models_path: Paths to the models to load
    :param dataset: Path to the dataset
    :param store_hypotheses: Path for storing the model hypotheses
    :param verbose: Verbosity level
    :return:
    """

    logging.info('Starting online training.')

    check_params(params)
    # Load data
    if dataset is None:
        dataset = build_dataset(params)
    params['INPUT_VOCABULARY_SIZE'] = dataset.vocabulary_len[params['INPUTS_IDS_DATASET'][0]]
    params['OUTPUT_VOCABULARY_SIZE'] = dataset.vocabulary_len[params['OUTPUTS_IDS_DATASET'][0]]

    # Load models
    if models_path is not None:
        logging.info('Loading models from %s' % str(models_path))
        model_instances = [TranslationModel(params,
                                            model_type=params['MODEL_TYPE'],
                                            verbose=params['VERBOSE'],
                                            model_name=params['MODEL_NAME'] + '_' + str(i),
                                            vocabularies=dataset.vocabulary,
                                            store_path=params['STORE_PATH'],
                                            set_optimizer=False,
                                            clear_dirs=False)
                           for i in range(len(models_path))]
        models = [updateModel(model, path, -1, full_path=True) for (model, path) in zip(model_instances, models_path)]
    else:
        raise BaseException('Online mode requires an already trained model!')

    # Set additional inputs to models if using a custom loss function
    trainer_models = build_online_models(models, params)
    if params['N_BEST_OPTIMIZER']:
        logging.info('Using N-best optimizer with metric %s' % params['OPTIMIZER_REGULARIZER'])

    # Apply model predictions
    params_prediction = {  # Decoding params
        'beam_size': params['BEAM_SIZE'],
        'maxlen': params['MAX_OUTPUT_TEXT_LEN_TEST'],
        'optimized_search': params['OPTIMIZED_SEARCH'],
        'model_inputs': params['INPUTS_IDS_MODEL'],
        'model_outputs': params['OUTPUTS_IDS_MODEL'],
        'dataset_inputs': params['INPUTS_IDS_DATASET'],
        'dataset_outputs': params['OUTPUTS_IDS_DATASET'],
        'search_pruning': params.get('SEARCH_PRUNING', False),
        'normalize_probs': params['NORMALIZE_SAMPLING'],
        'alpha_factor': params['ALPHA_FACTOR'],
        'pos_unk': params['POS_UNK'],
        'state_below_index': -1,
        'output_text_index': 0,
        'apply_detokenization': params['APPLY_DETOKENIZATION'],
        'detokenize_f': eval('dataset.' + params['DETOKENIZATION_METHOD']),
        'coverage_penalty': params.get('COVERAGE_PENALTY', False),
        'length_penalty': params.get('LENGTH_PENALTY', False),
        'length_norm_factor': params.get('LENGTH_NORM_FACTOR', 0.0),
        'coverage_norm_factor': params.get('COVERAGE_NORM_FACTOR', 0.0),
        'output_max_length_depending_on_x': params.get('MAXLEN_GIVEN_X', True),
        'output_max_length_depending_on_x_factor': params.get('MAXLEN_GIVEN_X_FACTOR', 3),
        'output_min_length_depending_on_x': params.get('MINLEN_GIVEN_X', True),
        'output_min_length_depending_on_x_factor': params.get('MINLEN_GIVEN_X_FACTOR', 2),
        'n_best_optimizer': params['N_BEST_OPTIMIZER'],
        'optimizer_regularizer': params['OPTIMIZER_REGULARIZER']
    }
    params_training = {  # Traning params
        'n_epochs': params['MAX_EPOCH'],
        'shuffle': False,
        'loss': params.get('LOSS', 'categorical_crossentropy'),
        'batch_size': params.get('BATCH_SIZE', 1),
        'homogeneous_batches': False,
        'optimizer': params.get('OPTIMIZER', 'SGD'),
        'lr': params.get('LR', 0.1),
        'lr_decay': params.get('LR_DECAY', None),
        'lr_gamma': params.get('LR_GAMMA', 1.),
        'epochs_for_save': -1,
        'verbose': verbose,
        'eval_on_sets': params['EVAL_ON_SETS_KERAS'],
        'n_parallel_loaders': params['PARALLEL_LOADERS'],
        'extra_callbacks': [],  # callbacks,
        'reload_epoch': 0,
        'epoch_offset': 0,
        'data_augmentation': params['DATA_AUGMENTATION'],
        'patience': params.get('PATIENCE', 0),
        'metric_check': params.get('STOP_METRIC', None),
        'eval_on_epochs': params.get('EVAL_EACH_EPOCHS', True),
        'each_n_epochs': params.get('EVAL_EACH', 1),
        'start_eval_on_epoch': params.get('START_EVAL_ON_EPOCH', 0),
        'additional_training_settings': {'k': params.get('K', 1),
                                         'tau': params.get('TAU', 1),
                                         'lambda': params.get('LAMBDA', 0.5),
                                         'c': params.get('C', 0.5),
                                         'd': params.get('D', 0.5)

                                         }
    }

    # Create sampler
    logging.info('Creating sampler...')
    beam_searcher = BeamSearchEnsemble(models, dataset, params_prediction,
                                       n_best=params['N_BEST_OPTIMIZER'],
                                       verbose=verbose)
    params_prediction = copy.copy(params_prediction)
    params_prediction['store_hypotheses'] = store_hypotheses

    # Create trainer
    logging.info('Creating trainer...')
    if params["USE_CUSTOM_LOSS"]:
        # Update params_training:
        params_training['use_custom_loss'] = params.get('USE_CUSTOM_LOSS', False)

    online_trainer = OnlineTrainer(trainer_models,
                                   dataset,
                                   beam_searcher,
                                   params_prediction,
                                   params_training,
                                   verbose=verbose)

    # Open new data
    ftrg = codecs.open(target_filename, 'r', encoding='utf-8')  # File with post-edited (or reference) sentences.

    target_lines = ftrg.read().split(u'\n')[:-1]
    ftrg.close()
    fsrc = codecs.open(source_filename, 'r', encoding='utf-8')  # File with source sentences.
    source_lines = fsrc.read().split(u'\n')[:-1]
    fsrc.close()
    # Trim files
    source_lines = source_lines[:-1] if source_lines[-1] == u'' else source_lines
    target_lines = target_lines[:-1] if target_lines[-1] == u'' else target_lines
    n_lines = len(source_lines)
    assert len(source_lines) == len(target_lines), 'Number of source and target lines must match'
    # Empty dest file
    if store_hypotheses:
        logging.info('Storing htypotheses in: %s' % store_hypotheses)
        codecs.open(store_hypotheses, 'w', encoding='utf-8').close()

    start_time = time.time()
    eta = -1
    for n_line, (source_line, target_line) in enumerate(zip(source_lines, target_lines)):

        src_seq = dataset.loadText([source_line.encode('utf-8')],
                                   dataset.vocabulary[params['INPUTS_IDS_DATASET'][0]],
                                   params['MAX_OUTPUT_TEXT_LEN_TEST'],
                                   0,
                                   fill=dataset.fill_text[params['INPUTS_IDS_DATASET'][0]],
                                   pad_on_batch=dataset.pad_on_batch[params['INPUTS_IDS_DATASET'][0]],
                                   words_so_far=False,
                                   loading_X=True)[0]
        if verbose > 1:
            logging.info('Input sentence:  %s' % str(src_seq))
            logging.info('Parsed sentence: %s' % str(
                map(lambda x: dataset.vocabulary[params['INPUTS_IDS_DATASET'][0]]['idx2words'][x], src_seq[0])))

        state_below = dataset.loadText([target_line.encode('utf-8')],
                                       dataset.vocabulary[params['OUTPUTS_IDS_DATASET'][0]],
                                       params['MAX_OUTPUT_TEXT_LEN_TEST'],
                                       1,
                                       fill=dataset.fill_text[params['INPUTS_IDS_DATASET'][-1]],
                                       pad_on_batch=dataset.pad_on_batch[params['INPUTS_IDS_DATASET'][-1]],
                                       words_so_far=False,
                                       loading_X=True)[0]
        trg_seq = dataset.loadTextOneHot([target_line.encode('utf-8')],
                                         vocabularies=dataset.vocabulary[params['OUTPUTS_IDS_DATASET'][0]],
                                         vocabulary_len=dataset.vocabulary_len[params['OUTPUTS_IDS_DATASET'][0]],
                                         max_len=params['MAX_OUTPUT_TEXT_LEN_TEST'],
                                         offset=0,
                                         fill=dataset.fill_text[params['OUTPUTS_IDS_DATASET'][0]],
                                         pad_on_batch=dataset.pad_on_batch[params['OUTPUTS_IDS_DATASET'][0]],
                                         words_so_far=False,
                                         sample_weights=params['SAMPLE_WEIGHTS'],
                                         loading_X=False)
        if verbose > 1:
            logging.info('Output sentence:  %s' % params_prediction['detokenize_f'](target_line))
            logging.info('Parsed sentence (state below): %s ' % map(
                lambda x: dataset.vocabulary[params['OUTPUTS_IDS_DATASET'][0]]['idx2words'][x], state_below[0]))

        online_trainer.sample_and_train_online([src_seq, state_below], trg_seq,
                                               src_words=[source_line],
                                               trg_words=[params_prediction['detokenize_f'](target_line)])
        sys.stdout.write('\r')
        sys.stdout.write("Processed %d/%d  -  ETA: %ds " % ((n_line + 1), n_lines, int(eta)))
        sys.stdout.flush()
        eta = (n_lines - (n_line + 1)) * (time.time() - start_time) / (n_line + 1)

    sys.stdout.write('The online training took: %f secs (Speed: %f sec/sample)\n' % ((time.time() - start_time), (
        time.time() - start_time) / n_lines))

    sys.stdout.write('We updated the model %d times for %d samples (%.3f%%)\n' %
                     (online_trainer.get_n_updates(), n_lines,
                      float(online_trainer.get_n_updates()) / n_lines))
    [saveModel(nmt_model, -1, path=params.get('STORE_PATH', 'retrained_model'), full_path=True)
     for nmt_model in models]


def buildCallbacks(params, model, dataset):
    """
    Builds the selected set of callbacks run during the training of the model.

    :param params: Dictionary of network hyperparameters.
    :param model: Model instance on which to apply the callback.
    :param dataset: Dataset instance on which to apply the callback.
    :return:
    """

    callbacks = []

    if params['METRICS'] or params['SAMPLE_ON_SETS']:
        # Evaluate training
        extra_vars = {'language': params.get('TRG_LAN', 'en'),
                      'n_parallel_loaders': params['PARALLEL_LOADERS'],
                      'tokenize_f': eval('dataset.' + params.get('TOKENIZATION_METHOD', 'tokenize_none')),
                      'detokenize_f': eval('dataset.' + params.get('DETOKENIZATION_METHOD', 'detokenize_none')),
                      'apply_detokenization': params.get('APPLY_DETOKENIZATION', False),
                      'tokenize_hypotheses': params.get('TOKENIZE_HYPOTHESES', True),
                      'tokenize_references': params.get('TOKENIZE_REFERENCES', True)
                      }

        input_text_id = params['INPUTS_IDS_DATASET'][0]
        vocab_x = dataset.vocabulary[input_text_id]['idx2words']
        vocab_y = dataset.vocabulary[params['OUTPUTS_IDS_DATASET'][0]]['idx2words']
        if params['BEAM_SEARCH']:
            extra_vars['beam_size'] = params.get('BEAM_SIZE', 6)
            extra_vars['state_below_index'] = params.get('BEAM_SEARCH_COND_INPUT', -1)
            extra_vars['maxlen'] = params.get('MAX_OUTPUT_TEXT_LEN_TEST', 30)
            extra_vars['optimized_search'] = params.get('OPTIMIZED_SEARCH', True)
            extra_vars['model_inputs'] = params['INPUTS_IDS_MODEL']
            extra_vars['model_outputs'] = params['OUTPUTS_IDS_MODEL']
            extra_vars['dataset_inputs'] = params['INPUTS_IDS_DATASET']
            extra_vars['dataset_outputs'] = params['OUTPUTS_IDS_DATASET']
            extra_vars['search_pruning'] = params.get('SEARCH_PRUNING', False)
            extra_vars['normalize_probs'] = params.get('NORMALIZE_SAMPLING', False)
            extra_vars['alpha_factor'] = params.get('ALPHA_FACTOR', 1.)
            extra_vars['coverage_penalty'] = params.get('COVERAGE_PENALTY', False)
            extra_vars['length_penalty'] = params.get('LENGTH_PENALTY', False)
            extra_vars['length_norm_factor'] = params.get('LENGTH_NORM_FACTOR', 0.0)
            extra_vars['coverage_norm_factor'] = params.get('COVERAGE_NORM_FACTOR', 0.0)
            extra_vars['state_below_maxlen'] = -1 if params.get('PAD_ON_BATCH', True) \
                else params.get('MAX_OUTPUT_TEXT_LEN', 50)
            extra_vars['pos_unk'] = params['POS_UNK']
            extra_vars['output_max_length_depending_on_x'] = params.get('MAXLEN_GIVEN_X', True)
            extra_vars['output_max_length_depending_on_x_factor'] = params.get('MAXLEN_GIVEN_X_FACTOR', 3)
            extra_vars['output_min_length_depending_on_x'] = params.get('MINLEN_GIVEN_X', True)
            extra_vars['output_min_length_depending_on_x_factor'] = params.get('MINLEN_GIVEN_X_FACTOR', 2)
            extra_vars['attend_on_output'] = params.get('ATTEND_ON_OUTPUT', 'transformer' in params['MODEL_TYPE'].lower())

            if params['POS_UNK']:
                extra_vars['heuristic'] = params['HEURISTIC']
                if params['HEURISTIC'] > 0:
                    extra_vars['mapping'] = dataset.mapping

        if params['METRICS']:
            for s in params['EVAL_ON_SETS']:
                extra_vars[s] = dict()
                extra_vars[s]['references'] = dataset.extra_variables[s][params['OUTPUTS_IDS_DATASET'][0]]
            callback_metric = PrintPerformanceMetricOnEpochEndOrEachNUpdates(model,
                                                                             dataset,
                                                                             gt_id=params['OUTPUTS_IDS_DATASET'][0],
                                                                             metric_name=params['METRICS'],
                                                                             set_name=params['EVAL_ON_SETS'],
                                                                             batch_size=params['BATCH_SIZE'],
                                                                             each_n_epochs=params['EVAL_EACH'],
                                                                             extra_vars=extra_vars,
                                                                             reload_epoch=params['RELOAD'],
                                                                             is_text=True,
                                                                             input_text_id=input_text_id,
                                                                             index2word_y=vocab_y,
                                                                             index2word_x=vocab_x,
                                                                             sampling_type=params['SAMPLING'],
                                                                             beam_search=params['BEAM_SEARCH'],
                                                                             save_path=model.model_path,
                                                                             start_eval_on_epoch=params[
                                                                                 'START_EVAL_ON_EPOCH'],
                                                                             write_samples=True,
                                                                             write_type=params['SAMPLING_SAVE_MODE'],
                                                                             eval_on_epochs=params['EVAL_EACH_EPOCHS'],
                                                                             save_each_evaluation=params[
                                                                                 'SAVE_EACH_EVALUATION'],
                                                                             verbose=params['VERBOSE'])

            callbacks.append(callback_metric)

        if params['SAMPLE_ON_SETS']:
            callback_sampling = SampleEachNUpdates(model,
                                                   dataset,
                                                   gt_id=params['OUTPUTS_IDS_DATASET'][0],
                                                   set_name=params['SAMPLE_ON_SETS'],
                                                   n_samples=params['N_SAMPLES'],
                                                   each_n_updates=params['SAMPLE_EACH_UPDATES'],
                                                   extra_vars=extra_vars,
                                                   reload_epoch=params['RELOAD'],
                                                   batch_size=params['BATCH_SIZE'],
                                                   is_text=True,
                                                   index2word_x=vocab_x,
                                                   index2word_y=vocab_y,
                                                   print_sources=True,
                                                   in_pred_idx=params['INPUTS_IDS_DATASET'][0],
                                                   sampling_type=params['SAMPLING'],  # text info
                                                   beam_search=params['BEAM_SEARCH'],
                                                   start_sampling_on_epoch=params['START_SAMPLING_ON_EPOCH'],
                                                   verbose=params['VERBOSE'])
            callbacks.append(callback_sampling)
    return callbacks


def check_params(params):
    """
    Checks some typical parameters and warns if something wrong was specified.
    :param params: Model instance on which to apply the callback.
    :return: None
    """

    if params['SRC_PRETRAINED_VECTORS'] and params['SRC_PRETRAINED_VECTORS'][:-1] != '.npy':
        warnings.warn('It seems that the pretrained word vectors provided for the target text are not in npy format.'
                      'You should preprocess the word embeddings with the "utils/preprocess_*_word_vectors.py script.')

    if params['TRG_PRETRAINED_VECTORS'] and params['TRG_PRETRAINED_VECTORS'][:-1] != '.npy':
        warnings.warn('It seems that the pretrained word vectors provided for the target text are not in npy format.'
                      'You should preprocess the word embeddings with the "utils/preprocess_*_word_vectors.py script.')
    if not params['PAD_ON_BATCH']:
        warnings.warn('It is HIGHLY recommended to set the option "PAD_ON_BATCH = True."')

    if params['MODEL_TYPE'].lower() == 'transformer':

        assert params['MODEL_SIZE'] == params['TARGET_TEXT_EMBEDDING_SIZE'], 'When using the Transformer model, ' \
                                                                             'dimensions of "MODEL_SIZE" and "TARGET_TEXT_EMBEDDING_SIZE" must match. ' \
                                                                             'Currently, they are: %d and %d, respectively.' % (params['MODEL_SIZE'], params['TARGET_TEXT_EMBEDDING_SIZE'])
        assert params['MODEL_SIZE'] == params['SOURCE_TEXT_EMBEDDING_SIZE'], 'When using the Transformer model, ' \
                                                                             'dimensions of "MODEL_SIZE" and "SOURCE_TEXT_EMBEDDING_SIZE" must match. ' \
                                                                             'Currently, they are: %d and %d, respectively.' % (params['MODEL_SIZE'], params['SOURCE_TEXT_EMBEDDING_SIZE'])
        if params['OPTIMIZED_SEARCH']:
            warnings.warn('The "OPTIMIZED_SEARCH" option is still untested for the "Transformer" model. Setting it to False.')
            params['OPTIMIZED_SEARCH'] = False

        if params['POS_UNK']:
            warnings.warn('The "POS_UNK" option is still unimplemented for the "Transformer" model. '
                          'Setting it to False.')
            params['POS_UNK'] = False
        assert params['MODEL_SIZE'] % params['N_HEADS'] == 0, \
            '"MODEL_SIZE" should be a multiple of "N_HEADS". ' \
            'Currently: mod(%d, %d) == %d.' % (params['MODEL_SIZE'], params['N_HEADS'], params['MODEL_SIZE'] % params['N_HEADS'])

    if params['POS_UNK']:
        assert params['OPTIMIZED_SEARCH'], 'Unknown words replacement requires ' \
                                           'to use the optimized search ("OPTIMIZED_SEARCH" parameter).'
    if params['COVERAGE_PENALTY']:
        assert params['OPTIMIZED_SEARCH'], 'The application of "COVERAGE_PENALTY" requires ' \
                                           'to use the optimized search ("OPTIMIZED_SEARCH" parameter).'
    return params

if __name__ == "__main__":
    args = parse_args()
    parameters = load_parameters()
    if args.config is not None:
        parameters = update_parameters(parameters, pkl2dict(args.config))

    if args.online:
        online_parameters = load_parameters_online()
        parameters = update_parameters(parameters, online_parameters)
    try:
        for arg in args.changes:
            try:
                k, v = arg.split('=')
            except ValueError:
                print ('Overwritten arguments must have the form key=Value. \n Currently are: %s' % str(args.changes))
                exit(1)
            try:
                parameters[k] = ast.literal_eval(v)
            except ValueError:
                parameters[k] = v
    except ValueError:
        print ('Error processing arguments: (', k, ",", v, ")")
        exit(2)

    parameters = check_params(parameters)
    if args.online:
        dataset = loadDataset(args.dataset)
        dataset = update_dataset_from_file(dataset, args.source, parameters,
                                           output_text_filename=args.references, splits=['train'], remove_outputs=False,
                                           compute_state_below=True)
        train_model_online(parameters, args.source, args.references, models_path=args.models, dataset=dataset,
                           store_hypotheses=args.hypotheses, verbose=args.verbose)

    elif parameters['MODE'] == 'training':
        logging.info('Running training.')
        train_model(parameters, args.dataset)
    elif parameters['MODE'] == 'sampling':
        logging.info('Running sampling.')
        apply_NMT_model(parameters, args.dataset)

    logging.info('Done!')
