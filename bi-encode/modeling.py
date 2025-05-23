import copy
import json
import logging
import os
from dataclasses import dataclass
from typing import Dict, Optional

import torch
import torch.distributed as dist
import torch.nn.functional as F
from bi_encoder.arguments import ModelArguments, \
    RetrieverTrainingArguments as TrainingArguments
from torch import nn, Tensor
from transformers import PreTrainedModel, AutoModel
from transformers.file_utils import ModelOutput

logger = logging.getLogger(__name__)


@dataclass
class EncoderOutput(ModelOutput):
    q_reps: Optional[Tensor] = None
    p_reps: Optional[Tensor] = None
    loss: Optional[Tensor] = None
    scores: Optional[Tensor] = None


class DensePooler(nn.Module):
    def __init__(self, input_dim: int = 768, output_dim: int = 768, tied=True):
        super(DensePooler, self).__init__()
        self.linear_q = nn.Linear(input_dim, output_dim)
        if tied:
            self.linear_p = self.linear_q
        else:
            self.linear_p = nn.Linear(input_dim, output_dim)
        self._config = {'input_dim': input_dim, 'output_dim': output_dim, 'tied': tied}

    def load(self, model_dir: str):
        pooler_path = os.path.join(model_dir, 'pooler.pt')
        if pooler_path is not None:
            if os.path.exists(pooler_path):
                logger.info(f'Loading Pooler from {pooler_path}')
                state_dict = torch.load(pooler_path, map_location='cpu')
                self.load_state_dict(state_dict)
                return
        logger.info("Training Pooler from scratch")
        return

    def save_pooler(self, save_path):
        torch.save(self.state_dict(), os.path.join(save_path, 'pooler.pt'))
        with open(os.path.join(save_path, 'pooler_config.json'), 'w') as f:
            json.dump(self._config, f)

    def forward(self, q: Tensor = None, p: Tensor = None, **kwargs):
        if q is not None:
            return self.linear_q(q)
        elif p is not None:
            return self.linear_p(p)
        else:
            raise ValueError


class BiEncoderModel(nn.Module):
    TRANSFORMER_CLS = AutoModel

    def __init__(self,
                 lm_q: PreTrainedModel,
                 lm_p: PreTrainedModel,
                 pooler: nn.Module = None,
                 untie_encoder: bool = False,
                 normlized: bool = False,
                 sentence_pooling_method: str = 'cls',
                 negatives_x_device: bool = False,
                 temperature: float = 1.0,
                 contrastive_loss_weight: float = 1.0,
                 loss_type: str = "softmax",
                 ):
        super().__init__()
        self.lm_q = lm_q
        self.lm_p = lm_p
        self.pooler = pooler
        self.cross_entropy = nn.CrossEntropyLoss(reduction='mean')
        self.kl = nn.KLDivLoss(reduction="mean")
        self.untie_encoder = untie_encoder

        self.normlized = normlized
        self.sentence_pooling_method = sentence_pooling_method
        self.temperature = temperature
        self.loss_type = loss_type
        # print(self.loss_type)
        self.negatives_x_device = negatives_x_device
        self.contrastive_loss_weight = contrastive_loss_weight
        if self.negatives_x_device:
            if not dist.is_initialized():
                raise ValueError('Distributed training has not been initialized for representation all gather.')
            self.process_rank = dist.get_rank()
            self.world_size = dist.get_world_size()

    def sentence_embedding(self, hidden_state, mask):
        if self.sentence_pooling_method == 'mean':  # 使用所有token的平均值
            s = torch.sum(hidden_state * mask.unsqueeze(-1).float(), dim=1)
            d = mask.sum(axis=1, keepdim=True).float()  # 计算每个句子的长度
            return s / d
        elif self.sentence_pooling_method == 'cls':  # 使用cls token
            return hidden_state[:, 0]

    def encode_passage(self, psg):
        if psg is None:
            return None
        psg_out = self.lm_p(**psg, return_dict=True)
        p_hidden = psg_out.last_hidden_state  # 取出最后一层的hidden state B * N * D
        p_reps = self.sentence_embedding(p_hidden, psg['attention_mask'])
        if self.pooler is not None:
            p_reps = self.pooler(p=p_reps)  # D * d
        if self.normlized:
            p_reps = torch.nn.functional.normalize(p_reps, dim=-1)
        return p_reps.contiguous()

    def encode_query(self, qry):
        if qry is None:
            return None
        qry_out = self.lm_q(**qry, return_dict=True)
        q_hidden = qry_out.last_hidden_state
        q_reps = self.sentence_embedding(q_hidden, qry['attention_mask'])
        if self.pooler is not None:
            q_reps = self.pooler(q=q_reps)
        if self.normlized:
            q_reps = torch.nn.functional.normalize(q_reps, dim=-1)
        return q_reps.contiguous()

    def compute_similarity(self, q_reps, p_reps):
        if len(p_reps.size()) == 2:  # 如果是单个句子，即没有batch维度
            return torch.matmul(q_reps, p_reps.transpose(0, 1))
        return torch.matmul(q_reps, p_reps.transpose(-2, -1))

    @staticmethod
    def load_pooler(model_weights_file, **config):
        pooler = DensePooler(**config)
        pooler.load(model_weights_file)
        return pooler

    @staticmethod
    def build_pooler(model_args):
        pooler = DensePooler(
            model_args.projection_in_dim,
            model_args.projection_out_dim,
            tied=not model_args.untie_encoder
        )
        pooler.load(model_args.model_name_or_path)
        return pooler
    def compute_loss(self, scores, target):        
        if self.loss_type == 'softmax':
            return self.cross_entropy(scores, target)
        elif self.loss_type == 'multi-softmax':  # L2 loss
            # 多个正例的cross entropy, scores: B*C, target: B*C
            cross_entropy_batch = nn.CrossEntropyLoss(reduction='none')
            loss = cross_entropy_batch(scores, target.float())
            if (loss != 0.0).int().sum() == 0:  # batch内都是0
                return 0 * loss.sum()
            else:
                loss = loss.sum() / (loss != 0.0).int().sum()  # 针对样本数量取均值
                return loss
        elif self.loss_type == 'myloss':  # L3 loss
            # loss = -log(sum(exp(positives) / sum(exp(all)))
            # target indicates the position of positive samples, 1 for positive, 0 for negative
            valid_mask = target.sum(dim=1) > 0
            valid_scores = scores[valid_mask]
            valid_target = target[valid_mask]
            if valid_scores.size(0) == 0:
                return torch.tensor(0.0, device=scores.device, requires_grad=True)
            
            valid_target = valid_target.float()  # TODO: 根据是binary的标签还是210的标签选择是否除2
            soft_scores = F.softmax(valid_scores, dim=-1)
            sum_positives = torch.sum(soft_scores * valid_target, dim=-1)
            sum_positives = torch.clamp(sum_positives, min=1e-9, max=1.0)
            
            log_sum_scores = -torch.log(sum_positives)
            loss = torch.mean(log_sum_scores)
            return loss
        else:
            assert 1 > 2
    def forward(self, query: Dict[str, Tensor] = None, passage: Dict[str, Tensor] = None, teacher_score: Tensor = None):
        q_reps = self.encode_query(query)
        p_reps = self.encode_passage(passage)
        # print(teacher_score)
        # assert 1 > 2
        # if self.loss_type == "multi-softma" or "myloss":
        # assert teacher_score == None
        # for inference
        if q_reps is None or p_reps is None:  # 如果有一个为None，说明是用模型来编码
            return EncoderOutput(
                q_reps=q_reps,
                p_reps=p_reps,
                loss=None,
                scores=None
            )

        if self.training:
            kl_loss = 0.0
            if teacher_score is not None:
                if self.negatives_x_device:
                    q_reps = self._dist_gather_tensor(q_reps)
                    p_reps = self._dist_gather_tensor(p_reps)
                    teacher_score = self._dist_gather_tensor(teacher_score)
                        
                scores = self.compute_similarity(q_reps, p_reps)
                scores = scores / self.temperature
                scores = scores.view(q_reps.size(0), -1)
                
                # 多个正样本，每个query的正负样本由teacher score指出
                target = torch.zeros_like(scores, device=scores.device, dtype=torch.float32)
                # 填充标签
                B, N = teacher_score.shape
                indices = torch.arange(B, device=scores.device).unsqueeze(1) * N + torch.arange(N, device=scores.device)
                target = target.scatter_(1, indices, teacher_score)

                loss = self.compute_loss(scores, target)
            else:
                if self.negatives_x_device:
                    q_reps = self._dist_gather_tensor(q_reps)
                    p_reps = self._dist_gather_tensor(p_reps)

                scores = self.compute_similarity(q_reps, p_reps)
                scores = scores / self.temperature
                scores = scores.view(q_reps.size(0), -1)

                target = torch.arange(scores.size(0), device=scores.device, dtype=torch.long)
                target = target * (p_reps.size(0) // q_reps.size(0))

                loss = self.compute_loss(scores, target)
        else:
            scores = self.compute_similarity(q_reps, p_reps)
            loss = None
        return EncoderOutput(
            loss=loss,
            scores=scores,
            q_reps=q_reps,
            p_reps=p_reps,
        )

    # def compute_loss(self, scores, target):
    #     return self.cross_entropy(scores, target)

    def _dist_gather_tensor(self, t: Optional[torch.Tensor]):
        if t is None:
            return None
        t = t.contiguous()

        all_tensors = [torch.empty_like(t) for _ in range(self.world_size)]  # 进程数个tensor
        dist.all_gather(all_tensors, t)  # 将每个进程的tensor收集到all_tensors中

        all_tensors[self.process_rank] = t  # 将当前进程的tensor放到对应的位置
        all_tensors = torch.cat(all_tensors, dim=0)  # 拼接所有的tensor

        return all_tensors

    @classmethod
    def build(
            cls,
            model_args: ModelArguments,
            train_args: TrainingArguments,
            **hf_kwargs,
    ):
        # load local
        if os.path.isdir(model_args.model_name_or_path):
            if model_args.untie_encoder:

                _qry_model_path = os.path.join(model_args.model_name_or_path, 'query_model')
                _psg_model_path = os.path.join(model_args.model_name_or_path, 'passage_model')
                if not os.path.exists(_qry_model_path):
                    _qry_model_path = model_args.model_name_or_path
                    _psg_model_path = model_args.model_name_or_path
                logger.info(f'loading query model weight from {_qry_model_path}')
                lm_q = cls.TRANSFORMER_CLS.from_pretrained(
                    _qry_model_path,
                    **hf_kwargs
                )
                logger.info(f'loading passage model weight from {_psg_model_path}')
                lm_p = cls.TRANSFORMER_CLS.from_pretrained(
                    _psg_model_path,
                    **hf_kwargs
                )
            else:
                lm_q = cls.TRANSFORMER_CLS.from_pretrained(model_args.model_name_or_path, **hf_kwargs)
                lm_p = lm_q
        # load pre-trained
        else:
            lm_q = cls.TRANSFORMER_CLS.from_pretrained(model_args.model_name_or_path, **hf_kwargs)
            lm_p = copy.deepcopy(lm_q) if model_args.untie_encoder else lm_q

        if model_args.add_pooler:
            pooler = cls.build_pooler(model_args)
        else:
            pooler = None

        model = cls(
            lm_q=lm_q,
            lm_p=lm_p,
            pooler=pooler,
            negatives_x_device=train_args.negatives_x_device,
            untie_encoder=model_args.untie_encoder,
            normlized=model_args.normlized,
            sentence_pooling_method=model_args.sentence_pooling_method,
            temperature=train_args.temperature,
            contrastive_loss_weight=train_args.contrastive_loss_weight,
            loss_type=train_args.loss_type
        )
        return model

    @classmethod
    def load(
            cls,
            model_name_or_path,
            normlized,
            sentence_pooling_method,
            **hf_kwargs,
    ):
        # load local
        untie_encoder = True
        if os.path.isdir(model_name_or_path):
            _qry_model_path = os.path.join(model_name_or_path, 'query_model')
            _psg_model_path = os.path.join(model_name_or_path, 'passage_model')
            if os.path.exists(_qry_model_path):
                logger.info(f'found separate weight for query/passage encoders')
                logger.info(f'loading query model weight from {_qry_model_path}')
                lm_q = cls.TRANSFORMER_CLS.from_pretrained(
                    _qry_model_path,
                    **hf_kwargs
                )
                logger.info(f'loading passage model weight from {_psg_model_path}')
                lm_p = cls.TRANSFORMER_CLS.from_pretrained(
                    _psg_model_path,
                    **hf_kwargs
                )
                untie_encoder = False
            else:
                logger.info(f'try loading tied weight')
                logger.info(f'loading model weight from {model_name_or_path}')
                lm_q = cls.TRANSFORMER_CLS.from_pretrained(model_name_or_path, **hf_kwargs)
                lm_p = lm_q
        else:
            logger.info(f'try loading tied weight')
            logger.info(f'loading model weight from {model_name_or_path}')
            lm_q = cls.TRANSFORMER_CLS.from_pretrained(model_name_or_path, **hf_kwargs)
            lm_p = lm_q

        pooler_weights = os.path.join(model_name_or_path, 'pooler.pt')
        pooler_config = os.path.join(model_name_or_path, 'pooler_config.json')
        if os.path.exists(pooler_weights) and os.path.exists(pooler_config):
            logger.info(f'found pooler weight and configuration')
            with open(pooler_config) as f:
                pooler_config_dict = json.load(f)
            pooler = cls.load_pooler(model_name_or_path, **pooler_config_dict)
        else:
            pooler = None

        model = cls(
            lm_q=lm_q,
            lm_p=lm_p,
            pooler=pooler,
            untie_encoder=untie_encoder,
            normlized=normlized,
            sentence_pooling_method=sentence_pooling_method,
        )
        return model

    def save(self, output_dir: str):
        if self.untie_encoder:
            os.makedirs(os.path.join(output_dir, 'query_model'))
            os.makedirs(os.path.join(output_dir, 'passage_model'))
            self.lm_q.save_pretrained(os.path.join(output_dir, 'query_model'))
            self.lm_p.save_pretrained(os.path.join(output_dir, 'passage_model'))
        else:
            self.lm_q.save_pretrained(output_dir)
        if self.pooler:
            self.pooler.save_pooler(output_dir)


class MultiBiEncoderModel(nn.Module):
    TRANSFORMER_CLS = AutoModel

    def __init__(self,
                 lm_q: PreTrainedModel,
                 lm_p: PreTrainedModel,
                 pooler: nn.Module = None,
                 untie_encoder: bool = False,
                 normlized: bool = False,
                 sentence_pooling_method: str = 'cls',
                 negatives_x_device: bool = False,
                 temperature: float = 1.0,
                 contrastive_loss_weight: float = 1.0
                 ):
        super().__init__()
        self.lm_q = lm_q
        self.lm_p = lm_p
        self.pooler = pooler
        self.loss_func = nn.HingeEmbeddingLoss(margin=0.5, reduction='mean')
        self.kl = nn.KLDivLoss(reduction="mean")
        self.untie_encoder = untie_encoder

        self.normlized = normlized
        self.sentence_pooling_method = sentence_pooling_method
        self.temperature = temperature
        self.negatives_x_device = negatives_x_device
        self.contrastive_loss_weight = contrastive_loss_weight
        if self.negatives_x_device:
            if not dist.is_initialized():
                raise ValueError('Distributed training has not been initialized for representation all gather.')
            self.process_rank = dist.get_rank()
            self.world_size = dist.get_world_size()

    def sentence_embedding(self, hidden_state, mask):
        if self.sentence_pooling_method == 'mean':  # 使用所有token的平均值
            s = torch.sum(hidden_state * mask.unsqueeze(-1).float(), dim=1)
            d = mask.sum(axis=1, keepdim=True).float()  # 计算每个句子的长度
            return s / d
        elif self.sentence_pooling_method == 'cls':  # 使用cls token
            return hidden_state[:, 0]

    def encode_passage(self, psg):
        if psg is None:
            return None
        psg_out = self.lm_p(**psg, return_dict=True)
        p_hidden = psg_out.last_hidden_state  # 取出最后一层的hidden state B * N * D
        p_reps = self.sentence_embedding(p_hidden, psg['attention_mask'])
        if self.pooler is not None:
            p_reps = self.pooler(p=p_reps)  # D * d
        if self.normlized:
            p_reps = torch.nn.functional.normalize(p_reps, dim=-1)
        return p_reps.contiguous()

    def encode_query(self, qry):
        if qry is None:
            return None
        qry_out = self.lm_q(**qry, return_dict=True)
        q_hidden = qry_out.last_hidden_state
        q_reps = self.sentence_embedding(q_hidden, qry['attention_mask'])
        if self.pooler is not None:
            q_reps = self.pooler(q=q_reps)
        if self.normlized:
            q_reps = torch.nn.functional.normalize(q_reps, dim=-1)
        return q_reps.contiguous()

    def compute_similarity(self, q_reps, p_reps):
        if len(p_reps.size()) == 2:  # 如果是单个句子，即没有batch维度
            return torch.matmul(q_reps, p_reps.transpose(0, 1))
        return torch.matmul(q_reps, p_reps.transpose(-2, -1))

    @staticmethod
    def load_pooler(model_weights_file, **config):
        pooler = DensePooler(**config)
        pooler.load(model_weights_file)
        return pooler

    @staticmethod
    def build_pooler(model_args):
        pooler = DensePooler(
            model_args.projection_in_dim,
            model_args.projection_out_dim,
            tied=not model_args.untie_encoder
        )
        pooler.load(model_args.model_name_or_path)
        return pooler

    def forward(self, query: Dict[str, Tensor] = None, passage: Dict[str, Tensor] = None, teacher_score: Tensor = None):
        q_reps = self.encode_query(query)
        p_reps = self.encode_passage(passage)

        # for inference
        if q_reps is None or p_reps is None:  # 如果有一个为None，说明是用模型来编码
            return EncoderOutput(
                q_reps=q_reps,
                p_reps=p_reps,
                loss=None,
                scores=None
            )

        if self.training:
            if self.negatives_x_device:
                q_reps = self._dist_gather_tensor(q_reps)
                p_reps = self._dist_gather_tensor(p_reps)

            scores = self.compute_similarity(q_reps, p_reps)
            scores = scores / self.temperature
            scores = scores.view(q_reps.size(0), -1)

            # 多个正样本，每个query的正负样本由teacher score指出
            target = torch.zeros_like(scores, device=scores.device, dtype=torch.float32)
            # 填充标签
            B, N = teacher_score.shape
            indices = torch.arange(B, device=scores.device).unsqueeze(1) * N + torch.arange(N, device=scores.device)
            target = target.scatter_(1, indices, teacher_score)
            # # 设置正负样本的weight为比例的倒数，正样本为总数除以正样本数，负样本为总数除以负样本数
            # weights = torch.ones_like(target, device=scores.device)
            # for i in range(weights.size(0)):
            #     num_positives = torch.sum(target[i])
            #     num_negatives = weights.size(1) - num_positives
            #     if num_positives > 0:
            #         weights[i][target[i] == 1] = weights.size(1) / num_positives
            #     if num_negatives > 0:
            #         weights[i][target[i] == 0] = weights.size(1) / num_negatives

            loss = self.compute_loss(scores, target)

        else:
            scores = self.compute_similarity(q_reps, p_reps)
            loss = None
        return EncoderOutput(
            loss=loss,
            scores=scores,
            q_reps=q_reps,
            p_reps=p_reps,
        )

    def compute_loss(self, scores, target, weights=None):
        # scores: B * (B * N), target: B * (B * N)
        # loss = nn.BCEWithLogitsLoss(weight=weights)(scores, target)

        # loss = -log(sum(exp(positives) / sum(exp(all)))
        # target indicates the position of positive samples, 1 for positive, 0 for negative
        # valid_mask = target.sum(dim=1) > 0
        # valid_scores = scores[valid_mask]
        # valid_target = target[valid_mask]
        # if valid_scores.size(0) == 0:
        #     return torch.tensor(0.0, device=scores.device, requires_grad=True)

        # soft_scores = F.softmax(valid_scores, dim=-1)
        # sum_positives = torch.sum(soft_scores * valid_target, dim=-1)
        # log_sum_scores = -torch.log(sum_positives)
        # loss = torch.mean(log_sum_scores)

        # hinge embedding loss
        target = target * 2 - 1
        target = target.flatten()

        distance = 1 - scores
        distance = distance.flatten()

        loss = self.loss_func(distance, target)

        return loss
    
    def _dist_gather_tensor(self, t: Optional[torch.Tensor]):
        if t is None:
            return None
        t = t.contiguous()

        all_tensors = [torch.empty_like(t) for _ in range(self.world_size)]  # 进程数个tensor
        dist.all_gather(all_tensors, t)  # 将每个进程的tensor收集到all_tensors中

        all_tensors[self.process_rank] = t  # 将当前进程的tensor放到对应的位置
        all_tensors = torch.cat(all_tensors, dim=0)  # 拼接所有的tensor

        return all_tensors

    @classmethod
    def build(
            cls,
            model_args: ModelArguments,
            train_args: TrainingArguments,
            **hf_kwargs,
    ):
        # load local
        if os.path.isdir(model_args.model_name_or_path):
            if model_args.untie_encoder:

                _qry_model_path = os.path.join(model_args.model_name_or_path, 'query_model')
                _psg_model_path = os.path.join(model_args.model_name_or_path, 'passage_model')
                if not os.path.exists(_qry_model_path):
                    _qry_model_path = model_args.model_name_or_path
                    _psg_model_path = model_args.model_name_or_path
                logger.info(f'loading query model weight from {_qry_model_path}')
                lm_q = cls.TRANSFORMER_CLS.from_pretrained(
                    _qry_model_path,
                    **hf_kwargs
                )
                logger.info(f'loading passage model weight from {_psg_model_path}')
                lm_p = cls.TRANSFORMER_CLS.from_pretrained(
                    _psg_model_path,
                    **hf_kwargs
                )
            else:
                lm_q = cls.TRANSFORMER_CLS.from_pretrained(model_args.model_name_or_path, **hf_kwargs)
                lm_p = lm_q
        # load pre-trained
        else:
            lm_q = cls.TRANSFORMER_CLS.from_pretrained(model_args.model_name_or_path, **hf_kwargs)
            lm_p = copy.deepcopy(lm_q) if model_args.untie_encoder else lm_q

        if model_args.add_pooler:
            pooler = cls.build_pooler(model_args)
        else:
            pooler = None

        model = cls(
            lm_q=lm_q,
            lm_p=lm_p,
            pooler=pooler,
            negatives_x_device=train_args.negatives_x_device,
            untie_encoder=model_args.untie_encoder,
            normlized=model_args.normlized,
            sentence_pooling_method=model_args.sentence_pooling_method,
            temperature=train_args.temperature,
            contrastive_loss_weight=train_args.contrastive_loss_weight,
            loss_type=train_args.loss_type
        )
        return model

    @classmethod
    def load(
            cls,
            model_name_or_path,
            normlized,
            sentence_pooling_method,
            **hf_kwargs,
    ):
        # load local
        untie_encoder = True
        if os.path.isdir(model_name_or_path):
            _qry_model_path = os.path.join(model_name_or_path, 'query_model')
            _psg_model_path = os.path.join(model_name_or_path, 'passage_model')
            if os.path.exists(_qry_model_path):
                logger.info(f'found separate weight for query/passage encoders')
                logger.info(f'loading query model weight from {_qry_model_path}')
                lm_q = cls.TRANSFORMER_CLS.from_pretrained(
                    _qry_model_path,
                    **hf_kwargs
                )
                logger.info(f'loading passage model weight from {_psg_model_path}')
                lm_p = cls.TRANSFORMER_CLS.from_pretrained(
                    _psg_model_path,
                    **hf_kwargs
                )
                untie_encoder = False
            else:
                logger.info(f'try loading tied weight')
                logger.info(f'loading model weight from {model_name_or_path}')
                lm_q = cls.TRANSFORMER_CLS.from_pretrained(model_name_or_path, **hf_kwargs)
                lm_p = lm_q
        else:
            logger.info(f'try loading tied weight')
            logger.info(f'loading model weight from {model_name_or_path}')
            lm_q = cls.TRANSFORMER_CLS.from_pretrained(model_name_or_path, **hf_kwargs)
            lm_p = lm_q

        pooler_weights = os.path.join(model_name_or_path, 'pooler.pt')
        pooler_config = os.path.join(model_name_or_path, 'pooler_config.json')
        if os.path.exists(pooler_weights) and os.path.exists(pooler_config):
            logger.info(f'found pooler weight and configuration')
            with open(pooler_config) as f:
                pooler_config_dict = json.load(f)
            pooler = cls.load_pooler(model_name_or_path, **pooler_config_dict)
        else:
            pooler = None

        model = cls(
            lm_q=lm_q,
            lm_p=lm_p,
            pooler=pooler,
            untie_encoder=untie_encoder,
            normlized=normlized,
            sentence_pooling_method=sentence_pooling_method,
        )
        return model

    def save(self, output_dir: str):
        if self.untie_encoder:
            os.makedirs(os.path.join(output_dir, 'query_model'))
            os.makedirs(os.path.join(output_dir, 'passage_model'))
            self.lm_q.save_pretrained(os.path.join(output_dir, 'query_model'))
            self.lm_p.save_pretrained(os.path.join(output_dir, 'passage_model'))
        else:
            self.lm_q.save_pretrained(output_dir)
        if self.pooler:
            self.pooler.save_pooler(output_dir)
