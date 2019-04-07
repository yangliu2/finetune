import tensorflow as tf

from finetune.base import BaseModel
from finetune.encoding.target_encoders import Seq2SeqLabelEncoder
from finetune.input_pipeline import BasePipeline
from finetune.util.shapes import shape_list
from finetune.nn.target_blocks import language_model
from finetune.util.beam_search import beam_search


class S2SPipeline(BasePipeline):

    def feed_shape_type_def(self):
        TS = tf.TensorShape
        return (
            (
                {
                    "tokens": tf.int32,
                    "mask": tf.float32
                },
                tf.int32
            ),
            (
                {
                    "tokens": TS([self.config.max_length, 2]),
                    "mask": TS([self.config.max_length])
                },
                TS([self.config.max_length, 2])
            )
        )

    def _target_encoder(self):
        return Seq2SeqLabelEncoder(self.text_encoder, self.config.max_length)


class S2S(BaseModel):
    """ 
    Classifies a single document into 1 of N categories.

    :param config: A :py:class:`finetune.config.Settings` object or None (for default config).
    :param \**kwargs: key-value pairs of config items to override.
    """

    def _get_input_pipeline(self):
        pipeline = S2SPipeline(self.config)
#        self.config.pipeline = pipeline
        return pipeline

    def featurize(self, X):
        """
        Embeds inputs in learned feature space. Can be called before or after calling :meth:`finetune`.

        :param X: list or array of text to embed.
        :returns: np.array of features of shape (n_examples, embedding_size).
        """
        return super().featurize(X)

    def predict(self, X):
        """
        Produces a list of most likely class labels as determined by the fine-tuned model.

        :param X: list or array of text to embed.
        :returns: list of class labels.
        """
        return super().predict(X)

    def predict_proba(self, X):
        """
        Produces a probability distribution over classes for each example in X.

        :param X: list or array of text to embed.
        :returns: list of dictionaries.  Each dictionary maps from a class label to its assigned class probability.
        """
        return super().predict_proba(X)

    def finetune(self, X, Y=None, batch_size=None):
        """
        :param X: list or array of text.
        :param Y: integer or string-valued class labels.
        :param batch_size: integer number of examples per batch. When N_GPUS > 1, this number
                           corresponds to the number of training examples provided to each GPU.
        """
        return super().finetune(X, Y=Y, batch_size=batch_size)

    @staticmethod
    def _target_model(config, featurizer_state, targets, n_outputs, train=False, reuse=None, label_encoder=None, **kwargs):
        encoder = label_encoder.encoder
        if targets is not None:
            target_feat_state = config.base_model.get_featurizer(
                targets,
                encoder=encoder,
                config=config,
                train=train,
                encoder_state=featurizer_state
            )
            embed_weights = target_feat_state["embed_weights"]
            return language_model(
                X=targets,
                M=tf.sequence_mask(target_feat_state["pool_idx"] + 1, maxlen=shape_list(embed_weights)[1], dtype=tf.float32), # +1 because we want to predict the clf token as an eos token
                embed_weights=embed_weights,
                config=config,
                reuse=reuse, train=train,
                hidden=target_feat_state["sequence_features"]
            )
        else:
            # returns (decoded beams [batch_size, beam_size, decode_length]
            #          decoding probabilities [batch_size, beam_size])
            embed_weights = featurizer_state.pop("embed_weights")
            
            def symbols_to_logits_fn(input_symbols, i, state): #[batch_size, decoded_ids] to [batch_size, vocab_size]
                leng = shape_list(input_symbols)[1]
                leng = tf.Print(leng, ["INPUT SIZE IS", tf.shape(input_symbols)])
                pos_embed = encoder.vocab_size + tf.range(leng)
                pos_embed = tf.tile([pos_embed], [shape_list(input_symbols)[0], 1])
                inp = tf.pad(tf.stack([input_symbols, pos_embed], -1), [[0,0], [0, 1], [0, 0]] )
                target_feat_state = config.base_model.get_featurizer(
                    inp,
                    encoder=encoder,
                    config=config,
                    train=train,
                    encoder_state={**state["featurizer_state"], "embed_weights":embed_weights}
                )
                output_state = language_model(
                    X=inp,
                    M=tf.sequence_mask(target_feat_state["pool_idx"] + 1, maxlen=leng + 1, dtype=tf.float32), # +1 because we want to predict the clf token as an eos token
                    embed_weights=embed_weights[:encoder.vocab_size, :],
                    config=config,
                    reuse=reuse, train=train,
                    hidden=target_feat_state["sequence_features"] # deal with state
                )
                output_state["logits"] = tf.Print(output_state["logits"], [tf.argmax(output_state["logits"][:, i, :], -1)])
                return output_state["logits"][:, i, :], state

            beams, probs, _ = beam_search(
                symbols_to_logits_fn=symbols_to_logits_fn,
                initial_ids=tf.constant([encoder.start for _ in range(config.batch_size)], dtype=tf.int32),
                beam_size=config.beam_size,
                decode_length=config.max_length,
                vocab_size=encoder.vocab_size,
                alpha=config.beam_search_alpha,
                states={"featurizer_state": featurizer_state},
                eos_id=encoder.clf_token,
                stop_early=True,
                use_top_k_with_unique=True
            )
            return {
                "logits": beams[:, -1, :],  # TODO, currently just takes the first beam
                "losses": -1.0
            }

    def _predict_op(self, logits, **kwargs):
        return logits

    def _predict_proba_op(self, logits, **kwargs):
        return logits
