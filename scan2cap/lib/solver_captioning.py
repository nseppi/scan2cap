'''
File Created: Monday, 25th November 2019 1:35:30 pm
Author: Dave Zhenyu Chen (zhenyu.chen@tum.de)
'''

import os
import sys
import time
import torch
import numpy as np
from tqdm import tqdm
from tensorboardX import SummaryWriter


sys.path.append(os.path.join(os.getcwd(), "lib")) # HACK add the lib folder
from lib.config import CONF
from lib.loss_helper import caption_loss, attention_regularization
from utils.eta import decode_eta
from utils.utils_lstm import clip_gradient


ITER_REPORT_TEMPLATE = """
-------------------------------iter: [{epoch_id}: {iter_id}/{total_iter}]-------------------------------
[loss] train_loss: {train_loss}
[sco.] train_bleu4: {train_bleu4}
[sco.] train_meteor: {train_meteor}
[sco.] train_rouge: {train_rouge}
[sco.] train_cider: {train_cider}
[sco.] train_attention_max: {train_attention_max}
[sco.] train_attention_max: {train_attention_var}
[info] mean_fetch_time: {mean_fetch_time}s
[info] mean_forward_time: {mean_forward_time}s
[info] mean_backward_time: {mean_backward_time}s
[info] mean_eval_time: {mean_eval_time}s
[info] mean_iter_time: {mean_iter_time}s
[info] ETA: {eta_h}h {eta_m}m {eta_s}s
"""

EPOCH_REPORT_TEMPLATE = """
---------------------------------summary---------------------------------
[train] train_loss: {train_loss}
[train] train_bleu4: {train_bleu4}
[train] train_meteor: {train_meteor}
[train] train_rouge: {train_rouge}
[train] train_cider: {train_cider}
[train] train_attention_max: {train_attention_max}
[train] train_attention_var: {train_attention_var}
[val]   val_loss: {val_loss}
[val]   val_bleu4: {val_bleu4}
[val]   val_meteor: {val_meteor}
[val]   val_rouge: {val_rouge}
[val]   val_cider: {val_cider}
[val]   val_attention_max: {val_attention_max}
[val]   val_attention_var: {val_attention_var}

"""

BEST_REPORT_TEMPLATE = """
--------------------------------------best--------------------------------------
[best] epoch: {epoch}
[loss] loss: {loss}
[sco.] bleu4: {bleu4}
[sco.] meteor: {meteor}
[sco.] rouge: {rouge}
[sco.] cider: {cider}
[sco.] attention_max: {attention_max}
[sco.] attention_var: {attention_var}

"""

class SolverCaptioning():
    def __init__(self, model, config, dataloader, optimizer, stamp, vocabulary, attention=False, val_step=10, early_stopping=-1, only_val=False, gradient_clip=None):
        self.epoch = 0                    # set in __call__
        self.verbose = 0                  # set in __call__
        
        self.model = model
        self.config = config
        self.dataloader = dataloader
        self.optimizer = optimizer
        self.stamp = stamp
        self.val_step = val_step
        self.early_stopping = early_stopping
        self.no_improve = 0
        self.stop = False
        self.vocabulary = vocabulary
        self.attention = attention
        self.only_val = only_val
        self.gradient_clip = gradient_clip

        self.best = {
            "epoch": 0,
            "loss": float("inf"),
            "bleu4": -float("inf"),
            "meteor": -float("inf"),
            "rouge": -float("inf"),
            "cider": -float("inf"), 
            "attention_max": -float("inf"),
            "attention_max": -float("inf"), 
            "caption_ratio": -float("inf"),
        }

        # log
        # contains all necessary info for all phases
        self.log = {
            phase: {
                # info
                "forward": [],
                "backward": [],
                "eval": [],
                "fetch": [],
                "iter_time": [],
                # loss (float, not torch.cuda.FloatTensor)
                "loss": [],
                # scores (float, not torch.cuda.FloatTensor)
                "bleu4": [],
                "meteor": [],
                "rouge": [],
                "cider": [],
                "attention_max": [],
                "attention_var": [], 
                "caption_ratio": [], 

            } for phase in ["train", "val"]
        }

        if not self.only_val:
            # tensorboard
            os.makedirs(os.path.join(CONF.PATH.OUTPUT, stamp, "tensorboard/train"), exist_ok=True)
            os.makedirs(os.path.join(CONF.PATH.OUTPUT, stamp, "tensorboard/val"), exist_ok=True)
            self._log_writer = {
                "train": SummaryWriter(os.path.join(CONF.PATH.OUTPUT, stamp, "tensorboard/train")),
                "val": SummaryWriter(os.path.join(CONF.PATH.OUTPUT, stamp, "tensorboard/val"))
            }

        # training log
        log_path = os.path.join(CONF.PATH.OUTPUT, stamp, "log.txt")
        self.log_fout = open(log_path, "a")

        # private
        # only for internal access and temporary results
        self._running_log = {}
        self._global_iter_id = 0
        self._total_iter = {}             # set in __call__

        # templates
        self.__iter_report_template = ITER_REPORT_TEMPLATE
        self.__epoch_report_template = EPOCH_REPORT_TEMPLATE
        self.__best_report_template = BEST_REPORT_TEMPLATE

    def __call__(self, epoch, verbose):
        # setting
        self.epoch = epoch
        self.verbose = verbose
        self._total_iter["train"] = len(self.dataloader["train"]) * epoch
        self._total_iter["val"] = len(self.dataloader["val"]) * self.val_step

        if self.only_val:
            self._log("evaluating...")
            self._feed(self.dataloader["val"], "val", 0)
            self._log("finished")
            self._best_report()
            return
        
        for epoch_id in range(epoch):
            try:
                self._log("epoch {} starting...".format(epoch_id + 1))

                # feed 
                self._feed(self.dataloader["train"], "train", epoch_id)

                # save model
                self._log("saving last models...\n")
                model_root = os.path.join(CONF.PATH.OUTPUT, self.stamp)
                torch.save(self.model.state_dict(), os.path.join(model_root, "model_last.pth"))

                if self.stop:
                    break
                
            except KeyboardInterrupt:
                # finish training
                self._finish(epoch_id)
                exit()

        # finish training
        self._finish(epoch_id)

    def _log(self, info_str):
        self.log_fout.write(info_str + "\n")
        self.log_fout.flush()
        print(info_str)

    def _set_phase(self, phase):
        if phase == "train":
            self.model.train()
        elif phase == "val":
            self.model.eval()
        else:
            raise ValueError("invalid phase")

    def _forward(self, data_dict):
        data_dict = self.model(data_dict)

        return data_dict

    def _backward(self):
        # optimize
        self.optimizer.zero_grad()
        self._running_log["loss"].backward()
        if self.gradient_clip is not None:
            clip_gradient(self.optimizer, grad_clip=self.gradient_clip)
        self.optimizer.step()

    def _compute_loss(self, data_dict):
        _, data_dict = caption_loss(data_dict, self.vocabulary)
        if self.attention: data_dict = attention_regularization(data_dict, 0.5)

        # dump
        self._running_log["loss"] = data_dict["loss"]
      
    def _feed(self, dataloader, phase, epoch_id):
        # switch mode
        self._set_phase(phase)

        # Reset log
        for key in self.log[phase]:
            self.log[phase][key] = []

        # change dataloader
        dataloader = dataloader if phase == "train" else tqdm(dataloader)

        for data_dict in dataloader:
            # move to cuda
            for key in data_dict:
                data_dict[key] = data_dict[key].cuda()

            # initialize the running loss
            self._running_log = {
                # loss
                "loss": 0,
                # acc
                "bleu4": 0,
                "meteor": 0,
                "rouge": 0,
                "cider": 0, 
                "attention_max":0,
                "caption_ratio":0
            }

            # load
            self.log[phase]["fetch"].append(data_dict["load_time"].sum().item())

            with torch.autograd.set_detect_anomaly(True):
                # forward
                start = time.time()
                data_dict = self._forward(data_dict)
                self._compute_loss(data_dict)
                self.log[phase]["forward"].append(time.time() - start)

                # backward
                if phase == "train":
                    start = time.time()
                    self._backward()
                    self.log[phase]["backward"].append(time.time() - start)
            
            # eval
            start = time.time()
            self._eval(data_dict)
            self.log[phase]["eval"].append(time.time() - start)

            # record log
            self.log[phase]["loss"].append(self._running_log["loss"].item())

            self.log[phase]["bleu4"].append(self._running_log["bleu4"])
            self.log[phase]["meteor"].append(self._running_log["meteor"])
            self.log[phase]["rouge"].append(self._running_log["rouge"])
            self.log[phase]["cider"].append(self._running_log["cider"])
            self.log[phase]["attention_max"].append(self._running_log["attention_max"])
            self.log[phase]["attention_var"].append(self._running_log["attention_var"])
            self.log[phase]["caption_ratio"].append(self._running_log["caption_ratio"])
    
            # report
            if phase == "train":
                iter_time = self.log[phase]["fetch"][-1]
                iter_time += self.log[phase]["forward"][-1]
                iter_time += self.log[phase]["backward"][-1]
                iter_time += self.log[phase]["eval"][-1]
                self.log[phase]["iter_time"].append(iter_time)
                if (self._global_iter_id + 1) % self.verbose == 0:
                    self._train_report(epoch_id)

                # evaluation
                if self._global_iter_id % self.val_step == 0:
                    print("evaluating...")
                    # val
                    self._feed(self.dataloader["val"], "val", epoch_id)
                    self._dump_log("val")
                    self._set_phase("train")
                    self._epoch_report(epoch_id)

                # dump log
                self._dump_log("train")
                self._global_iter_id += 1

                if self.stop:
                    return


        # check best
        if phase == "val":
            cur_criterion = "bleu4"
            print("Number of sample points", str(len(self.log[phase][cur_criterion])))
            cur_best = np.mean(self.log[phase][cur_criterion])
            if cur_best > self.best[cur_criterion] or self.only_val:
                self._log("best {} achieved: {}".format(cur_criterion, cur_best))
                self._log("current train_loss: {}".format(np.mean(self.log["train"]["loss"])))
                self._log("current val_loss: {}".format(np.mean(self.log["val"]["loss"])))
                self.best["epoch"] = epoch_id + 1
                self.best["loss"] = np.mean(self.log[phase]["loss"])
                self.best["bleu4"] = np.mean(self.log[phase]["bleu4"])
                self.best["meteor"] = np.mean(self.log[phase]["meteor"])
                self.best["rouge"] = np.mean(self.log[phase]["rouge"])
                self.best["cider"] = np.mean(self.log[phase]["cider"])
                self.best["attention_max"] = np.mean(self.log[phase]["attention_max"])
                self.best["attention_var"] = np.mean(self.log[phase]["attention_var"])
                self.best["caption_ratio"] = np.mean(self.log[phase]["caption_ratio"])

                if self.only_val:
                    return

                # save model
                self._log("saving best models...\n")
                model_root = os.path.join(CONF.PATH.OUTPUT, self.stamp)
                torch.save(self.model.state_dict(), os.path.join(model_root, "model.pth"))
                self.no_improve = 0
            else:
                self.no_improve += 1
                self._log(f"no improvement for {self.no_improve} validations...\n")
                if self.early_stopping > 0 and self.no_improve >= self.early_stopping:
                    self.stop = True
                    self._log(f"early stopping because no improvements were achieved after {self.no_improve} validations...\n")

    def _eval(self, data_dict):
        # dump
        self._running_log["bleu4"] = data_dict["bleu4"]
        self._running_log["meteor"] = data_dict["meteor"]
        self._running_log["rouge"] = data_dict["rouge"]
        self._running_log["cider"] = data_dict["cider"]
        self._running_log["attention_max"] = data_dict["attention_max"]
        self._running_log["attention_var"] = data_dict["attention_var"]
        self._running_log["caption_ratio"] = data_dict["caption_ratio"]

    def _dump_log(self, phase):
        log = {
            "loss": ["loss"],
            "bleu4": ["bleu4"],
            "meteor": ["meteor"],
            "rouge": ["rouge"],
            "cider": ["cider"],
            "attention_max": ["attention_max"],
            "attention_var": ["attention_var"],
            "caption_ratio": ["caption_ratio"],
            
        }
        for key in log:
            for item in log[key]:
                self._log_writer[phase].add_scalar(
                    "{}/{}".format(key, item),
                    np.mean([v for v in self.log[phase][item]]),
                    self._global_iter_id
                )

    def _finish(self, epoch_id):
        # print best
        self._best_report()

        # save model
        self._log("saving last models...\n")
        model_root = os.path.join(CONF.PATH.OUTPUT, self.stamp)
        torch.save(self.model.state_dict(), os.path.join(model_root, "model_last.pth"))

        # export
        for phase in ["train", "val"]:
            self._log_writer[phase].export_scalars_to_json(os.path.join(CONF.PATH.OUTPUT, self.stamp, "tensorboard/{}".format(phase), "all_scalars.json"))
            self._log_writer[phase].close()

    def _train_report(self, epoch_id):
        # compute ETA
        fetch_time = self.log["train"]["fetch"]
        forward_time = self.log["train"]["forward"]
        backward_time = self.log["train"]["backward"]
        eval_time = self.log["train"]["eval"]
        iter_time = self.log["train"]["iter_time"]

        mean_train_time = np.mean(iter_time)
        mean_est_val_time = np.mean([fetch + forward for fetch, forward in zip(fetch_time, forward_time)])
        eta_sec = (self._total_iter["train"] - self._global_iter_id - 1) * mean_train_time
        eta_sec += len(self.dataloader["val"]) * np.ceil(self._total_iter["train"] / self.val_step) * mean_est_val_time
        eta = decode_eta(eta_sec)

        # print report
        iter_report = self.__iter_report_template.format(
            epoch_id=epoch_id + 1,
            iter_id=self._global_iter_id + 1,
            total_iter=self._total_iter["train"],
            train_loss=round(np.mean([v for v in self.log["train"]["loss"]]), 5),
            train_bleu4=round(np.mean([v for v in self.log["train"]["bleu4"]]), 5),
            train_meteor=round(np.mean([v for v in self.log["train"]["meteor"]]), 5),
            train_rouge=round(np.mean([v for v in self.log["train"]["rouge"]]), 5),
            train_cider=round(np.mean([v for v in self.log["train"]["cider"]]), 5),
            train_attention_max=round(np.mean([v for v in self.log["train"]["attention_max"]]), 5),
            train_attention_var=round(np.mean([v for v in self.log["train"]["attention_var"]]), 5),            
            mean_fetch_time=round(np.mean(fetch_time), 5),
            mean_forward_time=round(np.mean(forward_time), 5),
            mean_backward_time=round(np.mean(backward_time), 5),
            mean_eval_time=round(np.mean(eval_time), 5),
            mean_iter_time=round(np.mean(iter_time), 5),
            eta_h=eta["h"],
            eta_m=eta["m"],
            eta_s=eta["s"]
        )
        self._log(iter_report)

    def _epoch_report(self, epoch_id):
        self._log("epoch [{}/{}] done...".format(epoch_id+1, self.epoch))
        epoch_report = self.__epoch_report_template.format(
            train_loss=round(np.mean([v for v in self.log["train"]["loss"]]), 5),
            train_bleu4=round(np.mean([v for v in self.log["train"]["bleu4"]]), 5),
            train_meteor=round(np.mean([v for v in self.log["train"]["meteor"]]), 5),
            train_rouge=round(np.mean([v for v in self.log["train"]["rouge"]]), 5),
            train_cider=round(np.mean([v for v in self.log["train"]["cider"]]), 5),
            train_attention_max=round(np.mean([v for v in self.log["train"]["attention_max"]]), 5),
            train_attention_var=round(np.mean([v for v in self.log["train"]["attention_var"]]), 5),
            val_loss=round(np.mean([v for v in self.log["val"]["loss"]]), 5),
            val_bleu4=round(np.mean([v for v in self.log["val"]["bleu4"]]), 5),
            val_meteor=round(np.mean([v for v in self.log["val"]["meteor"]]), 5),
            val_rouge=round(np.mean([v for v in self.log["val"]["rouge"]]), 5),
            val_cider=round(np.mean([v for v in self.log["val"]["cider"]]), 5),
            val_attention_max=round(np.mean([v for v in self.log["val"]["attention_max"]]), 5),
            val_attention_var=round(np.mean([v for v in self.log["val"]["attention_var"]]), 5),
        )
        self._log(epoch_report)
    
    def _best_report(self):
        self._log("training completed...")
        best_report = self.__best_report_template.format(
            epoch=self.best["epoch"],
            loss=round(self.best["loss"], 5),
            bleu4=round(self.best["bleu4"], 5),
            meteor=round(self.best["meteor"], 5),
            rouge=round(self.best["rouge"], 5),
            cider=round(self.best["cider"], 5),
            attention_max=round(self.best["attention_max"], 5),
            attention_var=round(self.best["attention_max"], 5),

        )
        self._log(best_report)
        with open(os.path.join(CONF.PATH.OUTPUT, self.stamp, "best.txt"), "w") as f:
            f.write(best_report)