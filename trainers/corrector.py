from typing import Any, Dict, List, Optional, Tuple, Union

import torch
import torch.nn as nn

from models import JointEmbeddingTextEncoder
from models.model_utils import freeze_params
from run_args import TrainingArguments

from .base import BaseTrainer
from .inversion import InversionTrainer


class CorrectorTrainer(BaseTrainer):
    """Trains an encoder model to generate embeddings that recursively correct of an
    InversionTrainer.

    TODO don't assume that the encoder has to have the same tokenizer as the encoder_decoder
    or embedder model.
    """

    def __init__(
        self,
        model: JointEmbeddingTextEncoder,
        inversion_trainer: InversionTrainer,
        args: TrainingArguments,
    ):
        # Freeze other model params
        freeze_params(inversion_trainer.model)
        # We're training this corrector model to correct outputs from
        # a model trained & loaded via the inversion trainer.
        self.inversion_trainer = inversion_trainer
        self.inversion_trainer.model.use_frozen_embeddings_as_input = True
        super().__init__(
            model=model,
            args=args,
            train_dataset=self.inversion_trainer.train_dataset,
            eval_dataset=self.inversion_trainer.eval_dataset,
            data_collator=self.inversion_trainer.data_collator,
        )
        self.tokenizer = self.inversion_trainer.model.tokenizer
        self.embedder_tokenizer = self.inversion_trainer.model.embedder_tokenizer
        self.call_embedding_model = self.inversion_trainer.model.call_embedding_model
        # Need to train with same device as the inversion model to avoid weird errors.

        assert self.args.fp16 == self.inversion_trainer.args.fp16
        assert self.args.bf16 == self.inversion_trainer.args.bf16

    def generate(self, inputs: Dict, generation_kwargs: Dict) -> torch.Tensor:
        with torch.no_grad():
            frozen_embeddings = self.inversion_trainer.model.call_embedding_model(
                input_ids=inputs["embedder_input_ids"],
                attention_mask=inputs["embedder_attention_mask"],
            )
            new_embeddings = self.model(
                embedding=frozen_embeddings,
                input_ids=inputs["input_ids"],
                attention_mask=inputs["attention_mask"],
            )
        inputs["frozen_embeddings"] = new_embeddings

        return self.inversion_trainer.generate(
            inputs=inputs, generation_kwargs=generation_kwargs
        )

    def compute_loss(
        self,
        model: JointEmbeddingTextEncoder,
        inputs: Dict[str, torch.Tensor],
        return_outputs: bool = False,
    ) -> Union[Tuple[torch.Tensor, Dict[str, torch.Tensor]], torch.Tensor]:
        """Computes contrastive loss using model generations and real text."""
        batch_size, seq_length = inputs["input_ids"].shape

        fake_embedder_input_ids = torch.ones(
            (batch_size, seq_length), device=self.args.device
        )
        fake_embedder_attention_mask = torch.ones(
            (batch_size, seq_length), device=self.args.device
        )

        with torch.no_grad():
            frozen_embeddings = self.inversion_trainer.model.call_embedding_model(
                input_ids=inputs["embedder_input_ids"],
                attention_mask=inputs["embedder_attention_mask"],
            )
        # TODO: support generated outputs of varying length.
        hypothesis_input_ids = self.inversion_trainer.model.generate(
            inputs={
                "embedder_input_ids": fake_embedder_input_ids,
                "embedder_attention_mask": fake_embedder_attention_mask,
                "frozen_embeddings": frozen_embeddings,
            },
            generation_kwargs={
                "early_stopping": False,
                "num_beams": 1,
                "do_sample": False,
                "no_repeat_ngram_size": 3,
            },
        )
        eos_token_id = self.inversion_trainer.model.embedder_tokenizer.eos_token_id
        eos_tokens = (
            torch.ones((batch_size, 1), dtype=torch.long, device=self.args.device)
            * eos_token_id
        )
        hypothesis_input_ids = torch.cat(
            (hypothesis_input_ids[:, 1:], eos_tokens), dim=1
        )
        # get rid of EOS token, add BOS token.
        hypothesis_attention_mask = (
            hypothesis_input_ids != self.embedder_tokenizer.pad_token_id
        )

        # NOTE TO SELF: can't put embedder_input_ids here, that's cheating.
        new_embeddings = self.model(
            embedding=frozen_embeddings,
            input_ids=hypothesis_input_ids,
            attention_mask=hypothesis_attention_mask,
        )

        # TODO: support passing embedder_input_ids/attention_mask as None.
        outputs = self.inversion_trainer.model(
            embedder_input_ids=fake_embedder_input_ids,
            embedder_attention_mask=fake_embedder_attention_mask,
            labels=inputs["labels"],
            frozen_embeddings=new_embeddings,
        )
        return outputs.loss

    def prediction_step(
        self,
        model: nn.Module,
        inputs: Dict[str, Union[torch.Tensor, Any]],
        prediction_loss_only: bool,
        ignore_keys: Optional[List[str]] = None,
    ) -> Tuple[Optional[torch.Tensor], Optional[torch.Tensor], Optional[torch.Tensor]]:
        """
        Perform an evaluation step on `model` using `inputs`. Called during self.evalaute()
        """
        inputs = {key: value.to(self.args.device) for key, value in inputs.items()}
        with torch.no_grad():
            loss = self.compute_loss(model=model, inputs=inputs)

        logits, labels = None, None
        return loss, logits, labels