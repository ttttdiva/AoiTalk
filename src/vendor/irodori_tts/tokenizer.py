from collections.abc import Iterable, Mapping
import json
from pathlib import Path

import torch


_TOKENIZERS_BACKEND_CLASS = "TokenizersBackend"


def _read_local_tokenizer_config(path: Path) -> dict[str, object] | None:
    """Read a bundled tokenizer config without mutating the HF snapshot."""
    config_path = path / "tokenizer_config.json"
    if not config_path.is_file():
        return None
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _load_bundled_tokenizers_backend(
    path: Path,
    config: Mapping[str, object],
):
    """Load v5 ``TokenizersBackend`` assets with transformers 4.x.

    Irodori v4.1 bundles a plain ``tokenizer.json`` and advertises the
    transformers 5-only ``TokenizersBackend`` class.  Transformers 4.57 can
    consume the same tokenizers serialization through ``PreTrainedTokenizerFast``;
    constructing it directly avoids editing the shared HF snapshot or relying
    on a process-global tokenizer registry.
    """
    from transformers import PreTrainedTokenizerFast

    tokenizer_file = path / "tokenizer.json"
    if not tokenizer_file.is_file():
        raise FileNotFoundError(
            f"Bundled TokenizersBackend config has no tokenizer.json: {path}"
        )

    kwargs: dict[str, object] = {}
    for token_name in (
        "bos_token",
        "eos_token",
        "unk_token",
        "sep_token",
        "pad_token",
        "cls_token",
        "mask_token",
    ):
        token = config.get(token_name)
        if isinstance(token, str) and token:
            kwargs[token_name] = token
    model_max_length = config.get("model_max_length")
    if isinstance(model_max_length, (int, float)) and not isinstance(model_max_length, bool):
        kwargs["model_max_length"] = int(model_max_length)
    for field in ("padding_side", "truncation_side", "clean_up_tokenization_spaces"):
        value = config.get(field)
        if isinstance(value, (str, bool)):
            kwargs[field] = value

    try:
        return PreTrainedTokenizerFast(tokenizer_file=str(tokenizer_file), **kwargs)
    except TypeError:
        # A future transformers release may reject a newer optional config
        # field.  Retry with only the stable special-token arguments.
        stable_kwargs = {
            key: value
            for key, value in kwargs.items()
            if key.endswith("_token")
        }
        return PreTrainedTokenizerFast(
            tokenizer_file=str(tokenizer_file),
            **stable_kwargs,
        )


class PretrainedTextTokenizer:
    """
    Hugging Face tokenizer wrapper for text conditioning.
    - right-padding for stable positional behavior
    - optional explicit BOS prepend
    """

    def __init__(self, tokenizer, add_bos: bool = True) -> None:
        self.tokenizer = tokenizer
        self.add_bos = bool(add_bos)
        # TTS collator uses fixed-length right-padding; enforce this regardless of pretrained defaults.
        self.tokenizer.padding_side = "right"

        if self.tokenizer.pad_token_id is None:
            if self.tokenizer.eos_token_id is not None and self.tokenizer.eos_token is not None:
                self.tokenizer.pad_token = self.tokenizer.eos_token
            else:
                raise ValueError(
                    "Tokenizer has no pad_token_id (and no eos_token fallback). "
                    "Set a pad token before training/inference."
                )

        if self.add_bos and self.tokenizer.bos_token_id is None:
            raise ValueError("Tokenizer has no bos_token_id but add_bos=True.")

    @classmethod
    def from_pretrained(
        cls,
        repo_id: str,
        add_bos: bool = True,
        local_files_only: bool = False,
        revision: str | None = None,
        cache_dir: str | None = None,
    ) -> "PretrainedTextTokenizer":
        try:
            from transformers import AutoTokenizer
        except ImportError as exc:
            raise RuntimeError(
                "transformers is required for pretrained text tokenization. "
                "Install with `pip install transformers sentencepiece`."
            ) from exc

        try:
            tokenizer = AutoTokenizer.from_pretrained(
                repo_id,
                use_fast=True,
                trust_remote_code=False,
                local_files_only=local_files_only,
                revision=revision,
                cache_dir=cache_dir,
            )
        except ValueError as exc:
            # v4.x does not register transformers 5's TokenizersBackend.  Only
            # apply the direct tokenizer.json fallback to an explicitly local
            # bundled asset with that exact config marker; remote/v3 tokenizers
            # continue through AutoTokenizer unchanged.
            local_path = Path(repo_id).expanduser()
            config = (
                _read_local_tokenizer_config(local_path)
                if local_files_only and local_path.is_dir()
                else None
            )
            if not config or config.get("tokenizer_class") != _TOKENIZERS_BACKEND_CLASS:
                raise
            try:
                tokenizer = _load_bundled_tokenizers_backend(local_path, config)
            except Exception as fallback_exc:
                raise exc from fallback_exc
        return cls(tokenizer=tokenizer, add_bos=add_bos)

    @property
    def vocab_size(self) -> int:
        return int(len(self.tokenizer))

    @property
    def bos_token_id(self) -> int | None:
        return self.tokenizer.bos_token_id

    @property
    def pad_token_id(self) -> int:
        pad_id = self.tokenizer.pad_token_id
        if pad_id is None:
            raise RuntimeError("pad_token_id is unexpectedly None.")
        return int(pad_id)

    def encode(self, text: str, add_bos: bool | None = None) -> torch.Tensor:
        token_ids = self.tokenizer.encode(text, add_special_tokens=False)
        use_bos = self.add_bos if add_bos is None else bool(add_bos)
        if use_bos:
            bos_id = self.bos_token_id
            if bos_id is None:
                raise ValueError("Tokenizer has no bos_token_id but BOS prepend was requested.")
            token_ids.insert(0, int(bos_id))
        return torch.tensor(token_ids, dtype=torch.long)

    def batch_encode(
        self,
        texts: Iterable[str],
        max_length: int | None = None,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        texts = list(texts)
        if not texts:
            raise ValueError("texts must contain at least one item.")
        if max_length is None:
            encoded = [self.encode(t) for t in texts]
            max_length = max(max(x.numel(), 1) for x in encoded)
        if max_length <= 0:
            raise ValueError(f"max_length must be > 0, got {max_length}")

        if self.add_bos:
            bos_id = self.bos_token_id
            if bos_id is None:
                raise ValueError("Tokenizer has no bos_token_id but BOS prepend was requested.")
            if max_length == 1:
                batch = torch.full(
                    (len(texts), 1),
                    fill_value=int(bos_id),
                    dtype=torch.long,
                )
                mask = torch.ones((len(texts), 1), dtype=torch.bool)
                return batch, mask
            body_max_length = max_length - 1
        else:
            body_max_length = max_length

        encoded_batch = self.tokenizer(
            texts,
            add_special_tokens=False,
            padding="max_length",
            truncation=True,
            max_length=body_max_length,
            return_tensors="pt",
            return_attention_mask=True,
        )
        body_ids = encoded_batch["input_ids"].to(dtype=torch.long)
        body_mask = encoded_batch["attention_mask"].to(dtype=torch.bool)

        if not self.add_bos:
            return body_ids, body_mask

        batch = torch.full(
            (len(texts), max_length),
            fill_value=self.pad_token_id,
            dtype=torch.long,
        )
        mask = torch.zeros((len(texts), max_length), dtype=torch.bool)
        batch[:, 0] = int(self.bos_token_id)
        mask[:, 0] = True
        batch[:, 1:] = body_ids
        mask[:, 1:] = body_mask
        return batch, mask
