"""Copyright 2020-2021 Google LLC

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""
"""
    This is the loadable seq2seq trainer library that is
    in charge of training details, loss compute, and statistics.
    See train.py for a use case of this library.

    Note: To make this a general library, we implement *only*
          mechanism things here(i.e. what to do), and leave the strategy
          things to users(i.e. how to do it). Also see train.py(one of the
          users of this library) for the strategy things we do.
"""

from copy import deepcopy
import itertools
import torch
from torch import nn
import onmt.utils
from onmt.utils.logging import logger
import numpy as np
xxx =None
def build_trainer(opt, device_id, model, fields, optim, model_saver=None):
    """
    Simplify `Trainer` creation based on user `opt`s*

    Args:
        opt (:obj:`Namespace`): user options (usually from argument parsing)
        model (:obj:`onmt.models.NMTModel`): the model to train
        fields (dict): dict of fields
        optim (:obj:`onmt.utils.Optimizer`): optimizer used during training
        data_type (str): string describing the type of data
            e.g. "text", "img", "audio"
        model_saver(:obj:`onmt.models.ModelSaverBase`): the utility object
            used to save the model
    """ 
    tgt_field = dict(fields)["tgt"].base_field
    train_loss = onmt.utils.loss.build_loss_compute(model, tgt_field, opt)
    valid_loss = onmt.utils.loss.build_loss_compute(
        model, tgt_field, opt, train=False)
    global xxx
    xxx = tgt_field.vocab.itos
    #assert(False)
    trunc_size = opt.truncated_decoder  # Badly named...
    shard_size = opt.max_generator_batches if opt.model_dtype == 'fp32' else 0
    norm_method = opt.normalization
    grad_accum_count = opt.accum_count
    n_gpu = opt.world_size
    average_decay = opt.average_decay
    average_every = opt.average_every
    if device_id >= 0:
        gpu_rank = opt.gpu_ranks[device_id]
    else:
        gpu_rank = 0
        n_gpu = 0
    gpu_verbose_level = opt.gpu_verbose_level

    report_manager = onmt.utils.build_report_manager(opt)
    trainer = TrainerC(model, train_loss, valid_loss, optim, trunc_size,
                           shard_size, norm_method,
                           grad_accum_count, n_gpu, gpu_rank,
                           gpu_verbose_level, report_manager,
                           model_saver=model_saver if gpu_rank == 0 else None,
                           average_decay=average_decay,
                           average_every=average_every,
                           model_dtype=opt.model_dtype,shuffletags=opt.shuffletags)
    return trainer


class TrainerC(object):
    """
    Class that controls the training process.

    Args:
            model(:py:class:`onmt.models.model.NMTModel`): translation model
                to train
            train_loss(:obj:`onmt.utils.loss.LossComputeBase`):
               training loss computation
            valid_loss(:obj:`onmt.utils.loss.LossComputeBase`):
               training loss computation
            optim(:obj:`onmt.utils.optimizers.Optimizer`):
               the optimizer responsible for update
            trunc_size(int): length of truncated back propagation through time
            shard_size(int): compute loss in shards of this size for efficiency
            data_type(string): type of the source input: [text|img|audio]
            norm_method(string): normalization methods: [sents|tokens]
            grad_accum_count(int): accumulate gradients this many times.
            report_manager(:obj:`onmt.utils.ReportMgrBase`):
                the object that creates reports, or None
            model_saver(:obj:`onmt.models.ModelSaverBase`): the saver is
                used to save a checkpoint.
                Thus nothing will be saved if this parameter is None
    """

    def __init__(self, model, train_loss, valid_loss, optim,
                 trunc_size=0, shard_size=32,
                 norm_method="sents", grad_accum_count=1, n_gpu=1, gpu_rank=1,
                 gpu_verbose_level=0, report_manager=None, model_saver=None,
                 average_decay=0, average_every=1, model_dtype='fp32',shuffletags=False):
        # Basic attributes.
        self.model = model
        self.train_loss = train_loss
        self.valid_loss = valid_loss
        self.optim = optim
        self.trunc_size = trunc_size
        self.shard_size = shard_size
        self.norm_method = norm_method
        self.grad_accum_count = grad_accum_count
        self.n_gpu = n_gpu
        self.gpu_rank = gpu_rank
        self.gpu_verbose_level = gpu_verbose_level
        self.report_manager = report_manager
        self.model_saver = model_saver
        self.average_decay = average_decay
        self.moving_average = None
        self.average_every = average_every
        self.model_dtype = model_dtype#
        self.shuffletags =  shuffletags

        assert grad_accum_count > 0
        if grad_accum_count > 1:
            assert self.trunc_size == 0, \
                """To enable accumulated gradients,
                   you must disable target sequence truncating."""

        # Set model in training mode.
        self.model.train()

    def _accum_batches(self, iterator):
        batches = []
        normalization = 0
        for batch in iterator:
            batches.append(batch)
            if self.norm_method == "tokens":
                num_tokens = batch.tgt[1:, :, 0].ne(
                    self.train_loss.padding_idx).sum()
                normalization += num_tokens.item()
            else:
                normalization += batch.batch_size
            if len(batches) == self.grad_accum_count:
                yield batches, normalization
                batches = []
                normalization = 0
        if batches:
            yield batches, normalization

    def _update_average(self, step):
        if self.moving_average is None:
            copy_params = [params.detach().float()
                           for params in self.model.parameters()]
            self.moving_average = copy_params
        else:
            average_decay = max(self.average_decay,
                                1 - (step + 1)/(step + 10))
            for (i, avg), cpt in zip(enumerate(self.moving_average),
                                     self.model.parameters()):
                self.moving_average[i] = \
                    (1 - average_decay) * avg + \
                    cpt.detach().float() * average_decay

    def train(self,
              train_iters,
              train_steps,
              save_checkpoint_steps=5000,
              valid_iter=None,
              valid_steps=10000,
              smooth =0):



        self.criticloss = torch.nn.BCEWithLogitsLoss()


        """
        The main training loop by iterating over `train_iter` and possibly
        running validation on `valid_iter`.

        Args:
            train_iter: A generator that returns the next training batch.
            train_steps: Run training for this many iterations.
            save_checkpoint_steps: Save a checkpoint every this many
              iterations.
            valid_iter: A generator that returns the next validation batch.
            valid_steps: Run evaluation every this many iterations.

        Returns:
            The gathered statistics.
        """
        if valid_iter is None:
            logger.info('Start training loop without validation...')
        else:
            logger.info('Start training loop and validate every %d steps...',
                        valid_steps)
        import random

        total_statsS = [onmt.utils.Statistics(basename="-".join(xx[0])) for xx in (( train_iters))]
        report_statsS = [onmt.utils.Statistics(basename="-".join(xx[0])) for xx in (( train_iters))]
        self._start_report_manager(start_time=total_statsS[0].start_time)
        if self.n_gpu > 1:
            assert(False)
        if self.n_gpu > 1:
            train_iter = itertools.islice(train_iters, self.gpu_rank, None, self.n_gpu)


        import random 
        bns = [(xxx[0],self._accum_batches(xxx[1])) for xxx in  train_iters]
        i = -1
        step = -1
        #print (train_steps)
        layeronly = 1
        seenstep = set()
        while not (train_steps > 0 and step >= train_steps):
            i+=1
            

            
            for jjj,(tags,bn) in enumerate(bns):
  
                report_stats = report_statsS[jjj]
                total_stats = total_statsS[jjj]
                randomint = random.uniform(0,1)
                skip = 1- (float(1.0/report_stats.ppl()))
                if randomint > skip:
                    pass
                else:
                
                    (batches, normalization) = next(bn)
    

                    ntags = tags
                   

                    step = self.optim.training_step
                    p =min(1,(step-100)/220000)
                    sl  = max(0, 2. / (1. + np.exp(-10. * p)) - 1)
                    self.model.critic.lambda_=sl

                    if self.model.critic2 is not None:
                        self.model.critic2.lambda_= sl

                    if self.gpu_verbose_level > 1:
                        logger.info("GpuRank %d: index: %d", self.gpu_rank, i)
                    if self.gpu_verbose_level > 0:
                        logger.info("GpuRank %d: reduce_counter: %d \
                                    n_minibatch %d"
                                    % (self.gpu_rank, i + 1, len(batches)))

                    if self.n_gpu > 1:
                        normalization = sum(onmt.utils.distributed
                                            .all_gather_list
                                            (normalization))
           
                    self._gradient_accumulation(
                        batches, normalization, total_stats,
                        report_stats,ntags)

                    if self.average_decay > 0 and i % self.average_every == 0:
                        self._update_average(step)

                    report_stats = self._maybe_report_training(
                        step, train_steps,
                        self.optim.learning_rate(),
                        report_stats)



                if valid_iter is not None and step % valid_steps == 0 and step not in seenstep:
                    seenstep.add(step)

         
                    if self.gpu_verbose_level > 0:
                        logger.info('GpuRank %d: validate step %d'
                                    % (self.gpu_rank, step))
                    valid_stats = self.validate(
                        valid_iter, moving_average=self.moving_average)
                    if self.gpu_verbose_level > 0:
                        logger.info('GpuRank %d: gather valid stat \
                                    step %d' % (self.gpu_rank, step))
                    valid_stats = self._maybe_gather_stats(valid_stats)
                    if self.gpu_verbose_level > 0:
                        logger.info('GpuRank %d: report stat step %d'
                                    % (self.gpu_rank, step))
                    self._report_step(self.optim.learning_rate(),
                                      step, valid_stats=valid_stats)
                print 
                if (self.model_saver is not None
                    and (save_checkpoint_steps != 0
                         and step % save_checkpoint_steps == 0)):

                    self.model_saver.save(step, moving_average=self.moving_average)

        #print (step)

        if self.model_saver is not None:
            self.model_saver.save(step, moving_average=self.moving_average)
        return total_stats

    def validate(self, valid_iter, moving_average=None):
        """ Validate model.
            valid_iter: validate data iterator
        Returns:
            :obj:`nmt.Statistics`: validation loss statistics
        """
        if moving_average:
            valid_model = deepcopy(self.model)
            for avg, param in zip(self.moving_average,
                                  valid_model.parameters()):
                param.data = avg.data.half() if self.model_dtype == "fp16" \
                    else avg.data
        else:
            valid_model = self.model

        # Set model in validating mode.
        valid_model.eval()

        with torch.no_grad():
            stats = onmt.utils.Statistics()
            tags = valid_iter[0]
            valid_iter = valid_iter[1]
            for batch in valid_iter:
                src, src_lengths = batch.src if isinstance(batch.src, tuple) \
                                   else (batch.src, None)
                tgt = batch.tgt

                # F-prop through the model.
                outputs, attns = valid_model(src, tgt, src_lengths,tags)
  
                # Compute loss.
                _, batch_stats = self.valid_loss(batch, outputs, attns)

                # Update statistics.
                stats.update(batch_stats)

        if moving_average:
            del valid_model
        else:
            # Set model back to training mode.
            valid_model.train()

        return stats

    def _gradient_accumulation(self, true_batches, normalization, total_stats,
                               report_stats,tags,freezeit=False):
        if self.grad_accum_count > 1:
            self.optim.zero_grad()

        for batch in true_batches:

            src, src_lengths = batch.src if isinstance(batch.src, tuple) \
                else (batch.src, None)
            if src_lengths is not None:
                report_stats.n_src_words += src_lengths.sum().item()
            target_size = batch.tgt.size(0)
            # Truncated BPTT: reminder not compatible with accum > 1
            if self.trunc_size:
                trunc_size = self.trunc_size
            else:
                trunc_size = target_size

      

            tgt_outer = batch.tgt

            bptt = False
            for j in range(0, target_size-1, trunc_size):
                # 1. Create truncated target.
                tgt = tgt_outer[j: j + trunc_size]
                lang = tags[-1]
                tags =tags[:-1]
                if self.grad_accum_count == 1:
                    self.optim.zero_grad()
                outputs, attns,rep,rep2 = self.model(src, tgt, src_lengths, tags=tags,nograd=freezeit,bptt=bptt,dumpenc=True)
                bptt = True

                 
                rep = rep[:min(src_lengths),:,:] 
                rep = rep.view(-1,512)
                cpred = self.model.critic(rep)
                if int(1) == lang:
                    target2 = np.array([[0.95]*len(cpred)]).T
                else:
                    target2 = np.array([[0.05]*len(cpred)]).T
                target2 = torch.FloatTensor(target2).cuda()
                closs =  10*self.criticloss(cpred,target2)

                if self.model.critic2 is not None:
                      
                    rep2 = rep2[:min(src_lengths),:,:]
                    rep2 = rep2.view(-1,512)
                    cpred2 = self.model.critic2(rep2)
                    if  int(1)== tags[-1]:
                        target22 = np.array([[0.95]*len(cpred2)]).T
                    else:
                        target22 = np.array([[0.05]*len(cpred2)]).T
                        target22 = torch.FloatTensor(target22).cuda()
                    closs = (closs+ 10*self.criticloss(cpred2,target22))

                loss, batch_stats = self.train_loss(
                    batch,
                    outputs,
                    attns,
                    normalization=normalization,
                    shard_size=self.shard_size,
                    trunc_start=j,
                    trunc_size=trunc_size,criticloss=closs)
     
                if loss is not None:
                    print ("Not none")
                    self.optim.backward(loss)

                if closs is None:
                    total_stats.update(batch_stats,criticloss=float(0.0))
                    report_stats.update(batch_stats,criticloss=float(0.0))
                else:
                    total_stats.update(batch_stats,criticloss=float(closs.item()))
                    report_stats.update(batch_stats,criticloss=float(closs.item()))

                # 4. Update the parameters and statistics.
                if self.grad_accum_count == 1:
                    # Multi GPU gradient gather
                    if self.n_gpu > 1:
                        grads = [p.grad.data for p in self.model.parameters()
                                 if p.requires_grad
                                 and p.grad is not None]
                        onmt.utils.distributed.all_reduce_and_rescale_tensors(
                            grads, float(1))
                    self.optim.step()

                if tags[-1] ==int(2) and self.model.decoder2 is not None:

                    if self.model.decoder2.state is not None:
                        self.model.decoder2.detach_state()
                else:
                    if self.model.decoder.state is not None:
                        self.model.decoder.detach_state()

        # in case of multi step gradient accumulation,
        # update only after accum batches
        if self.grad_accum_count > 1:
            if self.n_gpu > 1:
                grads = [p.grad.data for p in self.model.parameters()
                         if p.requires_grad
                         and p.grad is not None]
                onmt.utils.distributed.all_reduce_and_rescale_tensors(
                    grads, float(1))
            self.optim.step()

    def _start_report_manager(self, start_time=None):
        """
        Simple function to start report manager (if any)
        """
        if self.report_manager is not None:
            if start_time is None:
                self.report_manager.start()
            else:
                self.report_manager.start_time = start_time

    def _maybe_gather_stats(self, stat):
        """
        Gather statistics in multi-processes cases

        Args:
            stat(:obj:onmt.utils.Statistics): a Statistics object to gather
                or None (it returns None in this case)

        Returns:
            stat: the updated (or unchanged) stat object
        """
        if stat is not None and self.n_gpu > 1:
            return onmt.utils.Statistics.all_gather_stats(stat)
        return stat

    def _maybe_report_training(self, step, num_steps, learning_rate,
                               report_stats):
        """
        Simple function to report training stats (if report_manager is set)
        see `onmt.utils.ReportManagerBase.report_training` for doc
        """
        if self.report_manager is not None:
            return self.report_manager.report_training(
                step, num_steps, learning_rate, report_stats,
                multigpu=self.n_gpu > 1)

    def _report_step(self, learning_rate, step, train_stats=None,
                     valid_stats=None):
        """
        Simple function to report stats (if report_manager is set)
        see `onmt.utils.ReportManagerBase.report_step` for doc
        """
        if self.report_manager is not None:
            return self.report_manager.report_step(
                learning_rate, step, train_stats=train_stats,
                valid_stats=valid_stats)
