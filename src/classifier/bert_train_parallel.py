#!/usr/bin/env python3
'''
Created on Jun 8, 2020

TODO:
   o Ensure equal distribution of labels in dataset
     splits. See https://stackoverflow.com/questions/50544730/how-do-i-split-a-custom-dataset-into-training-and-test-datasets

@author: paepcke
'''

from _collections import OrderedDict
import argparse
import csv
import datetime
import json
import os, sys
import random
import time
import warnings

import GPUtil
from apex import amp
from apex.parallel import DistributedDataParallel
from sklearn.metrics import accuracy_score
from sklearn.metrics import classification_report 
from sklearn.metrics import confusion_matrix 
from sklearn.metrics import matthews_corrcoef
from torch import nn, cuda
import torch
from transformers import AdamW, BertForSequenceClassification
from transformers import get_linear_schedule_with_warmup

from bert_feeder_dataloader import MultiprocessingDataloader
from bert_feeder_dataloader import SqliteDataLoader
from bert_feeder_dataset import SqliteDataset
from logging_service import LoggingService
import numpy as np
import torch.distributed as dist


# Mixed floating point facility (Automatic Mixed Precision)
# From Nvidia: https://nvidia.github.io/apex/amp.html
sys.path.append(os.path.dirname(__file__))



# For parallelism:

# ------------------------ Specialty Exceptions --------

class NoGPUAvailable(Exception):
    pass

class TrainError(Exception):
    # Error in the train/validate loop
    pass

# ------------------------ Main Class ----------------
class BertTrainer(object):
    '''
    For this task, we first want to modify the pre-trained 
    BERT model to give outputs for classification, and then
    we want to continue training the model on our dataset
    until that the entire model, end-to-end, is well-suited
    for our task. 
    
    Thankfully, the huggingface pytorch implementation 
    includes a set of interfaces designed for a variety
    of NLP tasks. Though these interfaces are all built 
    on top of a trained BERT model, each has different top 
    layers and output types designed to accomodate their specific 
    NLP task.  
    
    Here is the current list of classes provided for fine-tuning:
    * BertModel
    * BertForPreTraining
    * BertForMaskedLM
    * BertForNextSentencePrediction
    * **BertForSequenceClassification** - The one we'll use.
    * BertForTokenClassification
    * BertForQuestionAnswering
    
    The documentation for these can be found under 
    https://huggingface.co/transformers/v2.2.0/model_doc/bert.html
    
    '''
#     SPACE_TO_COMMA_PAT = re.compile(r'([0-9])[\s]+')
    
    RANDOM_SEED = 3631
    # Modify the following for different, or
    # additional labels (needs testing):
#     LABEL_ENCODINGS=OrderedDict({'right'  : 0,
#                                  'left'   : 1,
#                                  'neutral': 2})
    LABEL_ENCODINGS=OrderedDict({'0'  : 0,
                                 '1'   : 1})

    # Automatic Mixed Precision optimization setting:
    
    #   "00"  Pure 32-bit floating point (F32), therefore a no-op
    #            Use to get lower speed, upper accuracy bound
    #   "01"  Recommended: Mixed precision, amp figures out 
    #           which methods should be run F16, and which F32
    #   "02"  Also a mix; but does not patch pytorch or Tensor funcs
    #   "03"  Pure F16. Fastest, but may not achieve stability.
    #           Try out to get upper speed, lower accuracy bounds
     
    AMP_OPTIMIZATION_LEVEL = "O1"
    
    # Device number for CPU (as opposed to GPUs, which 
    # are numbered as positive ints:
    CPU_DEV = -1
    
    #------------------------------------
    # Constructor 
    #-------------------

    def __init__(self,
                 csv_or_sqlite_path,
                 model_save_path=None,
                 text_col_name='text',
                 label_col_name='label',
                 epochs=4,
                 batch_size=32,
                 sequence_len=128,
                 learning_rate=3e-5,
                 label_encodings=None,
                 logfile=None,
                 preponly=False,
                 started_from_launch=False,
                 testing_cuda_on_cpu=False
                 ):
        '''
        Number of epochs: 2, 3, 4 
        '''
        #************
#         print(f"******WORLD_SIZE:{os.getenv('WORLD_SIZE')}")
#         print(f"******:RANK:{os.getenv('RANK')}")
#         print(f"******LOCAL_RANK:{os.getenv('LOCAL_RANK')}")
#         print(f"******:NODE_RANK:{os.getenv('NODE_RANK')}")
#         print(f"******:MASTER_ADDR:{os.getenv('MASTER_ADDR')}")
#         print(f"******:MASTER_PORT:{os.getenv('MASTER_PORT')}")
#         print("****** Exiting intentionally")
#         sys.exit()
        #************
        # Whether or not we are testing GPU related
        # code on a machine that only has a CPU.
        # Obviously: only set to True in that situation.
        # Don't use any computed results as real:
        self.testing_cuda_on_cpu = testing_cuda_on_cpu
        
        if label_encodings is None:
            self.label_encodings = self.LABEL_ENCODINGS
        else:
            self.label_encodings = label_encodings

        
        (file_root, ext) = os.path.splitext(csv_or_sqlite_path)

        # If told to preponly, but are passed an sqlite file,
        # rather than a csv file: treat as error
        if preponly and ext == '.sqlite':
            raise ValueError("Asked to proponly (i.e. create an sqlite db), but no .csv file was specified.")

        # If preponly, but an sqlite file exists, be cautious,
        # demand that user remove the sqlite file themselves:
        if preponly and os.path.exists(file_root + '.sqlite'):
            raise ValueError("Asked to proponly (i.e. create an sqlite db), but an sqlite file exists; remove that first.")

        dataset = self.create_dataset(csv_or_sqlite_path,
                                      self.label_encodings,
                                      text_col_name,
                                      label_col_name,
                                      sequence_len
                                      )
          
        # If only supposed to create the sqlite db, we are done
        if preponly:
            return

        try:
            self.local_rank = int(os.environ['LOCAL_RANK'])
        except KeyError:
            # We were not called via the launch.py script.
            # Check wether there is at least on 
            # local GPU, if so, use that:
            if len(GPUtil.getGPUs()) > 0:
                self.local_rank = 0
            else:
                self.local_rank = None
        
        if logfile is None:
            default_logfile_name = os.path.join(os.path.dirname(__file__), 
                                                'bert_train.log' if self.local_rank is None 
                                                else f'bert_train_{self.local_rank}.log'
                                                )
            self.log = LoggingService(logfile=default_logfile_name)
        elif logfile == 'stdout':
            self.log = LoggingService()
        else:
            # Logfile name provided by caller. Still
            # need to disambiguate between multiple processes,
            # if appropriate:
            if self.local_rank is not None:
                (logfile_root, ext) = os.path.splitext(logfile)
                logfile = f"{logfile_root}_{self.local_rank}{ext}"
            self.log = LoggingService(logfile=logfile)
        
        self.batch_size = batch_size
        self.epochs     = epochs
        
        # The following call also sets self.gpu_obj
        # to a GPUtil.GPU instance, so we can check
        # on the GPU status along the way:
        
        self.gpu_device = self.enable_GPU(self.local_rank)
        if self.gpu_device != self.CPU_DEV and \
            self.local_rank is not None:
            # We were launched via the launch.py script,
            # with local_rank indicating the GPU device
            # to use.
            # Internalize the promised env vars RANK and
            # WORLD_SIZE:
            try:
                self.node_rank = int(os.environ['NODE_RANK'])
                self.world_size = int(os.environ['WORLD_SIZE'])
                self.master_addr = os.environ['MASTER_ADDR']
                self.master_port = os.environ['MASTER_PORT']
                # If this script was launched manually, rather
                # than through the launch.py, and WORLD_SIZE is
                # greater than 1, the init_process_group() call
                # later on will hang, waiting for the remaining
                # sister processes. Therefore: if launched manually,
                # set WORLD_SIZE to 1, and RANK to 0:
                if not started_from_launch:
                    self.log.info(("Setting RANK to 0, and WORLD_SIZE to 1,\n"
                                   "b/c script was not started using launch.py()"
                                   ))
                    os.environ['RANK'] = '0'
                    os.environ['WORLD_SIZE'] = '1'
                    os.environ['MASTER_ADDR'] = '127.0.0.1'
                    self.node_rank  = 0
                    self.world_size = 1
                    self.master_addr = '127.0.0.1'
                    
            except KeyError as e:
                msg = (f"\nEnvironment variable {e.args[0]} not set.\n" 
                       "RANK, WORLD_SIZE, MASTER_ADDR, and MASTER_PORT\n"
                       "must be set.\n"
                       "Maybe use launch.py to run this script?"
                        )
                raise TrainError(msg)
            
            self.init_multiprocessing()
        else:
            # No GPU on this machine. Consider world_size
            # to be 1, which is the CPU struggling along:
            self.world_size = 1

        if model_save_path is None:
            (self.csv_file_root, _ext) = os.path.splitext(csv_or_sqlite_path)
            self.model_save_path = self.csv_file_root + '_trained_model' + '.sav'
        else:
            self.model_save_path = model_save_path
            
        # Preparation:

        if self.testing_cuda_on_cpu:
            self.cuda_dev   = 0
            self.gpu_device = 0
            self.world_size = 3
            self.node_rank  = 0

       
        if self.gpu_device == self.CPU_DEV:

            # CPU bound, single machine:

            self.train_dataloader = SqliteDataLoader(dataset.train_frozen_dataset,
                                                     batch_size=self.batch_size
                                                     )
            self.val_dataloader = SqliteDataLoader(dataset.validate_frozen_dataset,
                                                   batch_size=self.batch_size
                                                   )
                                                         
            self.test_dataloader = SqliteDataLoader(dataset.validate_frozen_dataset,
                                                    batch_size=self.batch_size
                                                    )
            
        else:
            # GPUSs used, single or multiple machines:
            
            self.train_dataloader = MultiprocessingDataloader(dataset.train_frozen_dataset,
                                                              self.world_size,
                                                              self.node_rank, 
                                                              batch_size=self.batch_size
                                                              )
            self.val_dataloader = MultiprocessingDataloader(dataset.validate_frozen_dataset,
                                                            self.world_size,
                                                            self.node_rank, 
                                                            batch_size=self.batch_size
                                                            )
            self.test_dataloader = MultiprocessingDataloader(dataset.test_frozen_dataset,
                                                             self.world_size,
                                                             self.node_rank, 
                                                             batch_size=self.batch_size
                                                             )
        if self.testing_cuda_on_cpu:
            self.gpu_device = self.CPU_DEV

        # TRAINING:        
        (self.model, self.optimizer, self.scheduler) = self.prepare_model(self.train_dataloader,
                                                                          learning_rate)
        self.train(epochs)
        
        # TESTING:

        (predictions, true_labels) = self.test()
        
        # EVALUATE RESULT:

        # If test set was empty:
        if predictions is None:
            self.log.info("No performance measures taken, because no predictions available.")
            return
        
        self.evaluate(predictions, true_labels)
        
#       Note: To maximize the score, we should now merge the 
#       validation set back into the train set, and retrain. 

    #------------------------------------
    # create_dataset
    #-------------------
    
    def create_dataset(self, 
                       csv_or_sqlite_path,
                       label_encodings,
                       text_col_name,
                       label_col_name,
                       sequence_len
                       ):
        '''
        Takes a csv or sqlite file pointer. If CSV,
        removes a possibly existing corresponding .sqlite
        file (same file path as the csv, but with .sqlite
        extension).
        
        If csv file, parse, tokenize, and create an sqlite
        db with the Bert word indices, and other data needed
        for training.
        
        @param csv_or_sqlite_path:
        @type csv_or_sqlite_path:
        @param text_col_name:
        @type text_col_name:
        @param label_col_name:
        @type label_col_name:
        @param sequence_len:
        @type sequence_len:
        @return: dataset instance
        @rtype: torch.DataSet
        '''
                       
        try:
            dataset = SqliteDataset(csv_or_sqlite_path,
                                    label_encodings,
                                    text_col_name=text_col_name,
                                    label_col_name=label_col_name,
                                    sequence_len=sequence_len,
                                    )
        except Exception as e:
            # Not recoverable
            raise IOError(f"Could not create dataset: {repr(e)}")

        # Save the label_encodings dict in a db table,
        # but reversed: int-code ==> label-str
        inverse_label_encs = OrderedDict()
        for (key, val) in self.label_encodings.items():
            inverse_label_encs[str(val)] = key
            
        dataset.save_dict_to_table('LabelEncodings', 
                                   inverse_label_encs, 
                                   delete_existing=True)

        # Split the dataset into train/validate/test,
        # and create a separate dataloader for each:
        dataset.split_dataset(train_percent=0.8,
                              val_percent=0.1,
                              test_percent=0.1,
                              random_seed=self.RANDOM_SEED)
        return dataset

    #------------------------------------
    # init_multiprocessing 
    #-------------------

    def init_multiprocessing(self):
        if self.node_rank == 0:
            self.log.info(f"Awaiting {self.world_size} nodes to run...")
        else:
            self.log.info("Awaiting master node's response...")
        dist.init_process_group(
            backend='nccl',
            init_method='env://'
        )
        self.log.info("And we're off!")

    #------------------------------------
    # enable_GPU 
    #-------------------

    def enable_GPU(self, local_rank, raise_gpu_unavailable=True):
        '''
        Returns the device id (an int) of an 
        available GPU. If none is exists on this 
        machine, returns self.CPU_DEV (-1).
        
        Initializes self.gpu_obj, which is a CUDA
        GPU instance. 
        
        Initializes self.cuda_dev: f"cuda:{device_id}"
        
        @param local_rank: which GPU to use. Created by the launch.py
            script. If None, just look for the next available GPU
        @type local_rank: {None|int|
        @param raise_gpu_unavailable: whether to raise error
            when GPUs exist on this machine, but none are available.
        @type raise_gpu_unavailable: bool
        @return: a GPU device ID, or self.CPU_DEV
        @rtype: int
        @raise NoGPUAvailable: if exception requested via 
            raise_gpu_unavailable, and no GPU is available.
        '''

        self.gpu_obj = None

        # Get the GPU device name.
        # Could be (GPU available):
        #   device(type='cuda', index=0)
        # or (No GPU available):
        #   device(type=self.CPU_DEV)

        gpu_objs = GPUtil.getGPUs()
        if len(gpu_objs) == 0:
            return self.CPU_DEV

        # GPUs are installed. Did caller ask for a 
        # specific GPU?
        
        if local_rank is not None:
            # Sanity check: did caller ask for a non-existing
            # GPU id?
            num_gpus = len(GPUtil.getGPUs())
            if num_gpus < local_rank + 1:
                # Definitely an error, don't revert to CPU:
                raise NoGPUAvailable(f"Request to use GPU {local_rank}, but only {num_gpus} available on this machine.")
            cuda.set_device(local_rank)
            # Part of the code uses self.cuda_dev
            self.cuda_dev = local_rank
            return local_rank
        
        # Caller did not ask for a specific GPU. Are any 
        # GPUs available, given their current memory/cpu 
        # usage? We use the default maxLoad of 0.5 and 
        # maxMemory of 0.5 as OK to use GPU:
        
        try:
            # If a GPU is available, the following returns
            # a one-element list of GPU ids. Else it throws
            # an error:
            device_id = GPUtil.getFirstAvailable()[0]
        except RuntimeError:
            # If caller wants non-availability of GPU
            # even though GPUs are installed to be an 
            # error, throw one:
            if raise_gpu_unavailable:
                raise NoGPUAvailable("Even though GPUs are installed, all are already in use.")
            else:
                # Else quietly revert to CPU
                return self.CPU_DEV
        
        # Get the GPU object that has the found
        # deviceID:
        self.gpu_obj_from_devid(device_id)
        
        # Initialize a string to use for moving 
        # tensors between GPU and cpu with their
        # to(device=...) method:
        self.cuda_dev = device_id 
        return device_id 

    #------------------------------------
    # gpu_obj_from_devid 
    #-------------------
    
    def gpu_obj_from_devid(self, devid):

        for gpu_obj in GPUtil.getGPUs():
            if gpu_obj.id == devid:
                self.gpu_obj = gpu_obj
                break
        return self.gpu_obj

    #------------------------------------
    # prepare_model 
    #-------------------

    def prepare_model(self, train_dataloader, learning_rate):
        '''
        - Batch size: no more than 8 for a 16GB GPU. Else 16, 32  
        - Learning rate (for the Adam optimizer): 5e-5, 3e-5, 2e-5  

        The epsilon parameter `eps = 1e-8` is "a very small 
        number to prevent any division by zero in the 
        implementation
        
        @param train_dataloader: data loader for model input data
        @type train_dataloader: DataLoader
        @param learning_rate: amount of weight modifications per cycle.
        @type learning_rate: float
        @return: model
        @rtype: BERT pretrained model
        @return: optimizer
        @rtype: Adam
        @return: scheduler
        @rtype: Schedule (?)
        '''
        
        # Load BertForSequenceClassification, the pretrained BERT model with a single 
        # linear classification layer on top. 
        model = BertForSequenceClassification.from_pretrained(
            #"bert-large-uncased", # Use the 12-layer BERT model, with an uncased vocab.
            "bert-base-uncased",
            num_labels = 3, # The number of output labels--2 for binary classification.
                            # You can increase this for multi-class tasks.   
            output_attentions = False, # Whether the model returns attentions weights.
            output_hidden_states = False, # Whether the model returns all hidden-states.
        )
        
        # Tell pytorch to run this model on the GPU.
        if self.gpu_device != self.CPU_DEV:
            model = model.to(device=self.cuda_dev)
        # Note: AdamW is a class from the huggingface library (as opposed to pytorch) 
        # I believe the 'W' stands for 'Weight Decay fix"
        optimizer = AdamW(model.parameters(),
                               #lr = 2e-5, # args.learning_rate - default is 5e-5, our notebook had 2e-5
                               lr = learning_rate,
                               eps = 1e-8 # args.adam_epsilon  - default is 1e-8.
                               )

        if self.gpu_device != self.CPU_DEV:
            # Allow Amp to perform casts as required by the opt_level
            # Second AMP related change:
            (model, optimizer) = amp.initialize(model, 
                                                optimizer, 
                                                opt_level=self.AMP_OPTIMIZATION_LEVEL)
            model = DistributedDataParallel(model)
        # Total number of training steps is [number of batches] x [number of epochs]. 
        # (Note that this is not the same as the number of training samples).
        total_steps = len(train_dataloader) * self.epochs
        
        # Create the learning rate scheduler.
        scheduler = get_linear_schedule_with_warmup(optimizer, 
                                                    num_warmup_steps = 0, # Default value in run_glue.py
                                                    num_training_steps = total_steps)
        return (model, optimizer, scheduler)

    #------------------------------------
    # train 
    #-------------------

    def train(self, epochs): 
        '''
        Below is our training loop. There's a lot going on, but fundamentally 
        for each pass in our loop we have a trianing phase and a validation phase. 
        
        **Training:**
        - Unpack our data inputs and labels
        - Load data onto the GPU for acceleration
        - Clear out the gradients calculated in the previous pass. 
            - In pytorch the gradients accumulate by default 
              (useful for things like RNNs) unless you explicitly clear them out.
        - Forward pass (feed input data through the network)
        - Backward pass (backpropagation)
        - Tell the network to update parameters with optimizer.sample_counter()
        - Track variables for monitoring progress
        
        **Evalution:**
        - Unpack our data inputs and labels
        - Load data onto the GPU for acceleration (if available)
        - Forward pass (feed input data through the network)
        - Compute train_loss on our validation data and track variables for monitoring progress
        
        Pytorch hides all of the detailed calculations from us, 
        but we've commented the code to point out which of the 
        above steps are happening on each line. 
        
        PyTorch also has some 
        https://pytorch.org/tutorials/beginner/blitz/cifar10_tutorial.html#sphx-glr-beginner-blitz-cifar10-tutorial-py) which you may also find helpful.*
        '''
        datetime.datetime.now().isoformat()
        
        # This training code is based on the `run_glue.py` script here:
        # https://github.com/huggingface/transformers/blob/5bfcd0485ece086ebcbed2d008813037968a9e58/examples/run_glue.py#L128
        
        # Set the seed value all over the place to make this reproducible.
        seed_val = self.RANDOM_SEED
        
        random.seed(seed_val)
        np.random.seed(seed_val)
        torch.manual_seed(seed_val)
        # From torch:
        cuda.manual_seed_all(seed_val)
        
        # We'll store a number of quantities such as training and validation train_loss, 
        # validation accuracy, and timings.
        self.training_stats = {'Training' : []}
        
        # Measure the total training time for the whole run.
        total_t0 = time.time()
        
        self.total_num_batches = round(len(self.train_dataloader) / (self.batch_size * self.world_size))
        
        # For each epoch...
        for epoch_i in range(0, epochs):
            
            # ========================================
            #               Training
            # ========================================
            
            # Perform one full pass over the training set.
        
            self.log.info("")
            self.log.info(f'======== Epoch :{epoch_i + 1} / {epochs} ========')
            self.log.info('Training...')
        
            # Measure how long the training epoch takes.
            t0 = time.time()

            if self.testing_cuda_on_cpu:
                self.gpu_device = 0

            if self.gpu_device != self.CPU_DEV:
                # Multiple GPUs are involved. Tell
                # the sampler that a new epoch is started,
                # so not all GPUs use the same order of
                # samples in each epoch:
                self.train_dataloader.set_epoch(epoch_i)

            # When using a GPU, we check GPU memory 
            # at critical moments, and store the result
            # as a dict in the following list:
            self.gpu_status_history = []

            (avg_train_loss, avg_train_accuracy) = self.train_one_epoch(epoch_i)
            
            # Measure how long this epoch took.
            training_time = self.format_time(time.time() - t0)
        
            self.log.info("")
            self.log.info(f"  Average training loss: {avg_train_loss:.2f}")
            self.log.info(f"  Average training accuracy: {avg_train_accuracy:.2f}")
            self.log.info(f"  Training epoch took: {training_time}")
                    
            # ========================================
            #               Validation
            # ========================================
            # After the completion of each training epoch, measure our performance on
            # our validation set.
            
            (avg_val_loss, avg_val_accuracy) = self.validate_one_epoch(epoch_i)
            
            # Record all statistics from this epoch.
            self.training_stats['Training'].append(
                {
                    'epoch': epoch_i + 1,
                    'Training Loss': avg_train_loss,
                    'Validation Loss': avg_val_loss,
                    'Training Accuracy': avg_train_accuracy,
                    'Validation Accuracy': avg_val_accuracy,
                }
            )
                
        self.log.info("")
        self.log.info("Training complete!")
        
        self.log.info("Total training took {:} (h:mm:ss)".format(self.format_time(time.time()-total_t0)))
        
        datetime.datetime.now().isoformat()

    #------------------------------------
    # train_one_epoch 
    #-------------------
    
    def train_one_epoch(self, epoch_i):
            # Reset the total train_loss for this epoch.
            total_train_loss = 0.0
            total_train_accuracy = 0.0
            t0 = time.time()


            # Put the model into training mode. Don't be misled--the call to 
            # `train` just changes the *mode*, it doesn't *perform* the training.
            # `dropout` and `batchnorm` layers behave differently during training
            # vs. test (source: https://stackoverflow.com/questions/51433378/what-does-model-train-do-in-pytorch)
            self.model.train()

            # For each batch of training data...
            # Tell data loader to pull from the train sample queue,
            # starting over:
            self.train_dataloader.reset()
            try:
                for sample_counter, batch in enumerate(self.train_dataloader):


                    # Progress update every 50 batches.
                    if sample_counter % 50 == 0 and not sample_counter == 0:
                        # Calculate elapsed time in minutes.
                        elapsed = self.format_time(time.time() - t0)
                        elapsed = self.log.info(f"Elapsed: {elapsed}") 
                        
                        # Report progress.
                        self.log_info(f"  Epoch: {epoch_i} Batch {sample_counter} of {self.total_num_batches}")
            
                    # Unpack this training batch from our dataloader. 
                    #
                    # As we unpack the batch, we'll also copy each tensor to the GPU using the 
                    # `to` method.
                    #
                    # `batch` contains three pytorch tensors:
                    #   [0]: input ids 
                    #   [1]: attention masks
                    #   [2]: labels

                    if self.testing_cuda_on_cpu:
                        self.gpu_device = self.CPU_DEV

                    if self.gpu_device == self.CPU_DEV:
                        b_input_ids = batch['tok_ids']
                        b_input_mask = batch['attention_mask']
                        b_labels = batch['label']
                    else:
                        b_input_ids = batch['tok_ids'].to(device=self.cuda_dev)
                        b_input_mask = batch['attention_mask'].to(device=self.cuda_dev)
                        b_labels = batch['label'].to(device=self.cuda_dev)
            
                    # Always clear any previously calculated gradients before performing a
                    # backward pass. PyTorch doesn't do this automatically because 
                    # accumulating the gradients is "convenient while training RNNs". 
                    # (source: https://stackoverflow.com/questions/48001598/why-do-we-need-to-call-zero-grad-in-pytorch)
                    self.model.zero_grad()        
            
                    # Note GPU usage:
                    if self.gpu_device != self.CPU_DEV:
                        self.history_checkpoint(epoch_i, sample_counter,'pre_model_call')
                        
                    # Perform a forward pass (evaluate the model on this training batch).
                    # The documentation for this `model` function is here: 
                    # https://huggingface.co/transformers/v2.2.0/model_doc/bert.html#transformers.BertForSequenceClassification
                    # It returns different numbers of parameters depending on what arguments
                    # arge given and what flags are set. For our useage here, it returns
                    # the train_loss (because we provided labels) and the "logits"--the model
                    # outputs prior to activation.
                    train_loss, logits = self.model(b_input_ids, 
                                                    token_type_ids=None, 
                                                    attention_mask=b_input_mask, 
                                                    labels=b_labels)
                    
                    if self.gpu_device != self.CPU_DEV:
                        b_input_ids = b_input_ids.to('cpu')
                        logits = logits.to('cpu')
                        b_labels = b_labels.to('cpu')
                        del b_input_mask
                        cuda.empty_cache()                    
    
                    train_acc = self.accuracy(logits, b_labels)
                    total_train_accuracy += train_acc
    
                    # Note GPU usage:
                    if self.gpu_device != self.CPU_DEV:
                        self.history_checkpoint(epoch_i, sample_counter,'post_model_call')
    
                    # Accumulate the training train_loss over all of the batches so that we can
                    # calculate the average train_loss at the end. `train_loss` is a Tensor containing a
                    # single value; the `.item()` function just returns the Python value 
                    # from the tensor.
                    total_train_loss += train_loss.item()
            
                    # Perform a backward pass to calculate the gradients.
                    if self.gpu_device != self.CPU_DEV:
                        # Third AMP related change:
                        with amp.scale_loss(train_loss, self.optimizer) as scaled_loss:
                            scaled_loss.backward()
                    else:
                        train_loss.backward()

                    # Clip the norm of the gradients to 1.0.
                    # This is to help prevent the "exploding gradients" problem.
                    # From torch:
                    nn.utils.clip_grad_norm_(self.model.parameters(), 1.0)
            
                    if self.gpu_device != self.CPU_DEV:
                        del b_input_ids
                        del b_labels
                        del train_loss
                        del logits
                        cuda.empty_cache()
                        self.history_checkpoint(epoch_i, sample_counter,'post_model_freeing')
     
    
                    # Update parameters and take a sample_counter using the computed gradient.
                    # The self.optimizer dictates the "update rule"--how the parameters are
                    # modified based on their gradients, the learning rate, etc.
                    
                    self.optimizer.step()
                    
                    # Note GPU usage:
                    if self.gpu_device != self.CPU_DEV:
                        cuda.empty_cache()
                        self.history_checkpoint(epoch_i, sample_counter,'post_optimizer')
                        
                    # Update the learning rate.
                    with warnings.catch_warnings(record=False):
                        # Suppress the expected warning about 
                        # lr_scheduler.step before optimizer.step()...
                        warnings.filterwarnings('ignore',
                                    message="Detected call of [`]lr_scheduler.step()[`]*",
                                    category=UserWarning)

                        self.scheduler.step()

                # Calculate the average loss over all of the batches.
                avg_train_loss = total_train_loss / len(self.train_dataloader)
                avg_train_accuracy = total_train_accuracy / len(self.train_dataloader)

            except Exception as e:
                msg = f"During train: {repr(e)}\n"
                    
                if self.gpu_device != self.CPU_DEV and self.gpu_obj is not None:
                    self.log.err(f"GPU memory used at crash time: {self.gpu_obj.memoryUsed}")
                    msg += "GPU use history:\n"
                    for chckpt_dict in self.gpu_status_history:
                        for event_info in chckpt_dict.keys():
                            msg += f"    {event_info}:      {chckpt_dict[event_info]}\n"
    
                raise TrainError(msg).with_traceback(e.__traceback__)
                    
            return(avg_train_loss, avg_train_accuracy)

    #------------------------------------
    # validate_one_epoch 
    #-------------------
    
    def validate_one_epoch(self, epoch_i):
        
        # If the number of samples were so few
        # that no validation set could be created,
        # do this:
        
        if len(self.val_dataloader) == 0:
            self.log.err("Validation set was empty, because too few samples. No validation done.")
            # Infinite loss, zero accuracy:
            return (np.inf, 0)
        
        try:
            self.log.info("")
            self.log.info("Running Validation...")
        
            t0 = time.time()
        
            # Put the model in evaluation mode--the dropout layers behave differently
            # during evaluation.
            self.model.eval()
        
            # Tracking variables 
            total_val_accuracy = 0
            total_val_loss = 0
            #nb_eval_steps = 0
        
            # Start feeding validation set from the beginning:
            self.val_dataloader.reset()
            # Evaluate data for one epoch
            for batch in self.val_dataloader:
                
                # Unpack this training batch from our dataloader. 
                #
                # As we unpack the batch, we'll also copy each tensor to the GPU using 
                # the `to` method.
                #
                # `batch` contains three pytorch tensors:
                #   [0]: input ids 
                #   [1]: attention masks
                #   [2]: labels
                if self.gpu_device == self.CPU_DEV:
                    b_input_ids = batch['tok_ids']
                    b_input_mask = batch['attention_mask']
                    b_labels = batch['label']
                else:
                    b_input_ids = batch['tok_ids'].to(device=self.cuda_dev)
                    b_input_mask = batch['attention_mask'].to(device=self.cuda_dev)
                    b_labels = batch['label'].to(device=self.cuda_dev)
                
                # Tell pytorch not to bother with constructing the compute graph during
                # the forward pass, since this is only needed for backprop (training).
                with torch.no_grad():        

                    # Forward pass, calculate logit predictions.
                    # token_type_ids is the same as the "segment ids", which 
                    # differentiates sentence 1 and 2 in 2-sentence tasks.
                    # The documentation for this `model` function is here: 
                    # https://huggingface.co/transformers/v2.2.0/model_doc/bert.html#transformers.BertForSequenceClassification
                    # Get the "logits" output by the model. The "logits" are the output
                    # values prior to applying an activation function like the softmax.
                    (val_loss, logits) = self.model(b_input_ids, 
                                                    token_type_ids=None, 
                                                    attention_mask=b_input_mask,
                                                    labels=b_labels)
                # Accumulate the validation loss and accuracy
                    total_val_loss += val_loss.item()

                if self.gpu_device != self.CPU_DEV:
                    logits = logits.to('cpu')
                    val_loss = val_loss.to('cpu')
                    b_labels = b_labels.to('cpu')
                try:
                    total_val_accuracy += self.accuracy(logits, b_labels)
                except TrainError as e:
                    self.log.err(f"In epoch {epoch_i} (when total_val_loss is {total_val_loss}: {repr(e)}")
                    raise TrainError from e

                if self.gpu_device != self.CPU_DEV:
                    del b_input_ids
                    del b_input_mask
                    del b_labels
                    del val_loss
                    del logits
                    cuda.empty_cache()

            # Calculate the average loss over all of the batches.
            avg_val_loss = total_val_loss / len(self.val_dataloader)
            avg_val_accuracy = total_val_accuracy / len(self.val_dataloader)
            
            # Measure how long the validation run took.
            validation_time = self.format_time(time.time() - t0)

            self.log.info(f"  Avg validation loss: {avg_val_loss:.2f}")
            self.log.info(f"  Avg validation accuracy: {avg_val_accuracy:.2f}")
            self.log.info(f"  Validation took: {validation_time}")
        
        except Exception as e:
            msg = f"During validate: {repr(e)}\n"

            if self.gpu_device != self.CPU_DEV  and self.gpu_obj is not None:
                self.log.err(f"GPU memory used at crash time: {self.gpu_obj.memoryUsed}")
                msg += "GPU use history:\n"
                for chckpt_dict in self.gpu_status_history:
                    for event_info in chckpt_dict.keys():
                        msg += f"    {event_info}:      {chckpt_dict[event_info]}\n"

            raise TrainError(msg).with_traceback(e.__traceback__)
            
        return(avg_val_loss, avg_val_accuracy)
    #------------------------------------
    # test 
    #-------------------

    def test(self):
        '''
        Apply our fine-tuned model to generate all_predictions on the test set.
        '''
        # If the number of samples were so few
        # that no training set could be created,
        # do this:
        
        if len(self.test_dataloader) == 0:
            self.log.err("Test set was empty, because too few samples. No test done.")
            # No predictions, no labels:
            return (None, None)

        # Put model in evaluation mode
        self.model.eval()
        
        # Tracking variables 
        all_predictions , all_labels = [], []

        if self.testing_cuda_on_cpu:
            self.gpu_device = self.CPU_DEV
        
        # Predict
        # Batches come as dicts with keys
        # sample_id, tok_ids, label, attention_mask: 

        for batch in self.test_dataloader:
             
            if self.gpu_device == self.CPU_DEV:
                b_input_ids = batch['tok_ids']
                b_input_mask = batch['attention_mask']
                b_labels = batch['label']
            else:
                b_input_ids = batch['tok_ids'].to(device=self.cuda_dev)
                b_input_mask = batch['attention_mask'].to(device=self.cuda_dev)
                b_labels = batch['label'].to(device=self.cuda_dev)
            
            # Telling the model not to compute or store gradients, saving memory and 
            # speeding up prediction
            with torch.no_grad():
                (loss, logits) = self.model(b_input_ids, 
                                            token_type_ids=None, 
                                            attention_mask=b_input_mask,
                                            labels=b_labels)

            if self.testing_cuda_on_cpu:
                self.gpu_device = self.CPU_DEV
                    
            # Move logits and labels to CPU, if the
            # are not already:
            if self.gpu_device != self.CPU_DEV:
                logits = logits.to('cpu')
                b_labels = b_labels.to('cpu')
                loss = loss.to('cpu')
                cuda.empty_cache()

            # Get the class prediction from the 
            # logits:
            predictions = self.logits_to_classes(logits)            
            # Store all_predictions and true labels
            all_predictions.extend(predictions)
            labels = b_labels.numpy()
            all_labels.extend(labels)
        
        self.log.info('    DONE applying model to test set.')
        # Ordered list of label ints:
        #matrix_labels = list(self.label_encodings.values())
        self.training_stats['Testing'] = \
                {
                    'Test Loss': float(loss),
                    'Test Accuracy': self.accuracy(all_predictions, all_labels),
                    'Matthews corrcoef': self.matthews_corrcoef(all_predictions, all_labels)
                    #'Confusion matrix' : self.confusion_matrix(all_predictions, 
                    #                                           all_labels, 
                    #                                           matrix_labels=matrix_labels
                    #                                           )}
                }

        if self.testing_cuda_on_cpu:
            self.gpu_device = self.CPU_DEV

        if self.gpu_device != self.CPU_DEV:
            del loss
            cuda.empty_cache()

        return(all_predictions, all_labels)

    #------------------------------------
    # evaluate 
    #-------------------

    def evaluate(self, predictions, labels):
        with warnings.catch_warnings(record=False):
            # Suppress the expected warning about the
            # double scalars:
            warnings.filterwarnings('ignore',
                                    message='invalid value encountered in double_scalars',
                                    category=RuntimeWarning)
            mcc = self.matthews_corrcoef(predictions, labels)
        self.log.info(f"Matthews correlation coefficient: {mcc}")

        test_accuracy = self.accuracy(predictions, labels)
        self.log.info(f"Accuracy on test set: {test_accuracy}")
        
        # Save the model:
        
        self.log.info(f"Saving model to {self.model_save_path} ...")
        with open(self.model_save_path, 'wb') as fd:
            #torch.save(model, fd)
            torch.save(self.model.state_dict(), fd)

        # Save the test predictions and true labels:
        predictions_path = f"{self.csv_file_root}_testset_predictions.csv"
        self.log.info(f"Saving predictions to {predictions_path}")

        with open(predictions_path, 'w') as fd:
            writer = csv.writer(fd)
            writer.writerow(['prediction', 'true_label'])
            for pred_truth_tuple in zip(predictions, labels):
                writer.writerow(pred_truth_tuple)
        
        # Save the training stats:
        training_stats_path = f"{self.csv_file_root}_train_test_stats.json"
        with open(training_stats_path, 'w') as fd:
            json.dump(self.training_stats, fd)


    #------------------------------------
    # matthews_corrcoef
    #-------------------

    def matthews_corrcoef(self, predicted_classes, labels):
        '''
        Computes the Matthew's correlation coefficient
        from arrays of predicted classes and true labels.
        The predicted_classes may be either raw logits, or
        an array of already processed logits, namely 
        class labels. Ex:
        
           [[logit1,logit2,logit3],
            [logit1,logit2,logit3]
            ]
            
        or: [class1, class2, ...]
        
        where the former are the log odds of each
        class. The latter are the classes decided by
        the highes-logit class. See self.logits_to_classes()
            
        
        @param predicted_classes: array of predicted class labels
        @type predicted_classes: {[int]|[[float]]}
        @param labels: array of true labels
        @type labels: [int]
        @return: Matthew's correlation coefficient,
        @rtype: float
        '''
        if type(predicted_classes) == torch.Tensor and \
            len(predicted_classes.shape) > 1:
            predicted_classes = self.logits_to_classes(predicted_classes)
        mcc = matthews_corrcoef(labels, predicted_classes)
        return mcc
        
        
    #------------------------------------
    # confusion_matrix 
    #-------------------
    
    def confusion_matrix(self, 
                         logits_or_classes, 
                         y_true,
                         matrix_labels=None
                         ):

        '''
        Print and return the confusion matrix
        '''
        # Format confusion matrix:
             
        #             right   left    neutral
        #     right
        #     left
        #     neutral

        if matrix_labels is None:
            matrix_labels = self.label_encodings.values()
            
        if type(logits_or_classes) == torch.Tensor and \
            len(logits_or_classes.shape) > 1:
            predicted_classes = self.logits_to_classes(logits_or_classes)
        else:
            predicted_classes = logits_or_classes
         
        n_by_n_conf_matrix = confusion_matrix(y_true, 
                                              predicted_classes, 
                                              labels=matrix_labels
                                              ) 
           
        self.log.info('Confusion Matrix :')
        self.log.info(n_by_n_conf_matrix) 
        self.log.info(f'Accuracy Score :{accuracy_score(y_true, predicted_classes)}')
        self.log.info('Report : ')
        self.log.info(classification_report(y_true, predicted_classes))
        
        return n_by_n_conf_matrix


# ---------------------- Utilities ----------------------

    #------------------------------------
    # prepare_model_save 
    #-------------------
    
    def prepare_model_save(self, model_file):
        if os.path.exists(model_file):
            print(f"File {model_file} exists")
            print("If intent is to load it, go to cell 'Start Here...'")
            self.log.info("Else remove on google drive, or change model_file name")
            self.log.info("Removal instructions: Either execute 'os.remove(model_file)', or do it in Google Drive")
            sys.exit(1)

        # File does not exist. But ensure that all the 
        # directories on the way exist:
        paths = os.path.dirname(model_file)
        try:
            os.makedirs(paths)
        except FileExistsError:
            pass

#     #------------------------------------
#     # to_np_array 
#     #-------------------
# 
#     def to_np_array(self, array_string):
#         # Use the pattern to substitute occurrences of
#         # "123   45" with "123,45". The \1 refers to the
#         # digit that matched (i.e. the capture group):
#         proper_array_str = PoliticalLeaningsAnalyst.SPACE_TO_COMMA_PAT.sub(r'\1,', array_string)
#         # Remove extraneous spaces:
#         proper_array_str = re.sub('\s', '', proper_array_str)
#         # Turn from a string to array:
#         return np.array(ast.literal_eval(proper_array_str))

    #------------------------------------
    # accuracy 
    #-------------------

    def accuracy(self, predicted_classes, labels):
        '''
        Function to calculate the accuracy of our predictions vs labels.
        The predicted_classes may be either raw logits, or
        an array of already processed logits, namely 
        class labels. Ex:
        
           [[logit1,logit2,logit3],
            [logit1,logit2,logit3]
            ]
            
        or: [class1, class2, ...]
        
        where the former are the log odds of each
        class. The latter are the classes decided by
        the highes-logit class. See self.logits_to_classes()

        Accuracy is returned as percentage of times the
        prediction agreed with the labels.
        
        @param predicted_classes: raw predictions: for each sample: logit for each class 
        @type predicted_classes:
        @param labels: for each sample: true class
        @type labels: [int]
        '''
        # Convert logits to classe predictions if needed:
        if type(predicted_classes) == torch.Tensor and \
            len(predicted_classes.shape) > 1:
            # This call will also move the result to CPU:
            predicted_classes = self.logits_to_classes(predicted_classes)
        if type(labels) != np.ndarray:
            if type(labels) == list:
                labels = np.array(labels)
            else:
                labels = labels.numpy()            
        # Compute number of times prediction was equal to the label.
        return np.count_nonzero(predicted_classes == labels) / len(labels)

    #------------------------------------
    # logits_to_classes 
    #-------------------
    
    def logits_to_classes(self, logits):
        '''
        Given an array of logit arrays, return
        an array of classes. Example:
        for a three-class classifier, logits might
        be:
           [[0.4, 0.6, -1.4],
            [-4.0,0.7,0.9]
           ]
        The first says: logit of first sample being class 0 is 0.4.
        To come up with a final prediction for each sample,
        pick the highest logit label as the prediction (0,1,...):
          row1: label == 1
          row2: label == 2

        @param logits: array of class logits
        @type logits: [[float]] or tensor([[float]])
        '''
        # Run amax on every sample's logits array,
        # generating a class in place of each array.
        # The detach() is needed to get
        # just the tensors, without the gradient function
        # from the tensor+grad. The [0] is needed, b/c
        # the np.nonzero() returns a tuple whose only
        # element is a one-valued array.
        # There *must* be a more elegant way to do this!
        pred_classes = np.argmax(logits.detach().numpy(), 1) 
        return pred_classes

    #------------------------------------
    # format_time  
    #-------------------

    def format_time(self, elapsed):
        '''
        Helper function for formatting elapsed times as `hh:mm:ss`
        Takes a time in seconds and returns a string hh:mm:ss
        '''
    
        # Round to the nearest second.
        elapsed_rounded = int(round((elapsed)))
        
        # Format as hh:mm:ss
        return str(datetime.timedelta(seconds=elapsed_rounded))

    #------------------------------------
    # history_checkpoint 
    #-------------------

    def history_checkpoint(self, epoch_num, sample_counter, process_moment):
        #**********
        return
        #**********
        # Note GPU usage:
        self.gpu_status_history.append(
            {'epoch_num'      : epoch_num,
             'sample_counter' : sample_counter,
             'process_moment' : process_moment,
             'GPU_free_memory': self.gpu_obj.memoryFree,
             'GPU_memory_used': self.gpu_obj.memoryUsed
             }
            )

# -------------------- Main ----------------
if __name__ == '__main__':

    data_dir = os.path.join(os.path.dirname(__file__), 'datasets')
    
    parser = argparse.ArgumentParser(prog=os.path.basename(sys.argv[0]),
                                     formatter_class=argparse.RawTextHelpFormatter,
                                     description="BERT-train from CSV, or run saved model."
                                     )

    parser.add_argument('-f', '--logfile',
                        help="path to log file; if 'stdout' direct to display;\n" +\
                             'Default: facebook_ads.log in script file.',
                        default=None);
    parser.add_argument('-t', '--text',
                        help="name of column with text (default 'text')",
                        default='text'
                        )
    parser.add_argument('-l', '--labels',
                        help="name of column with the true labels (default: 'label')",
                        default='label'
                        )
    parser.add_argument('-e', '--epochs',
                        type=int,
                        help="number of epochs to run through (default: 3)",
                        default=3
                        )
    parser.add_argument('-p','--preponly',
                        action='store_true',
                        help="only read csv file, and create sqlite",
                        default=False
                        )
    parser.add_argument('--started_from_launch',
                        action='store_true',
                        help="Used only by launch.py script! Indicate that script started via launch.py",
                        default=False
                        )

    parser.add_argument('data_source_path',
                        help='path to csv or sqlite file to process')

    args = parser.parse_args();
    _pa = BertTrainer(args.data_source_path,
                     text_col_name=args.text,
                     label_col_name=args.labels,
                     epochs=args.epochs,
                     learning_rate=2e-5,
                     batch_size=32,
                     logfile=args.logfile,
                     preponly=args.preponly,
                     started_from_launch=args.started_from_launch,
                     testing_cuda_on_cpu=False
                     )
         
    
