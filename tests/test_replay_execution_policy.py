import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import localnet_replay_corpus as replay
from localnet_e2e_support import E2EError

POLICY = {
    "execution_revision": "xvm-execution-3",
    "gas_schedule": "xvm-gas-2",
    "bytecode_version": "xvm-1",
    "set_codec": "xian-set-1",
}


def test_current_replay_requires_matching_image_policy():
    replay.validate_replay_policy({"execution_policy": POLICY}, {"execution_policy": POLICY})
    with pytest.raises(E2EError, match="execution policy"):
        replay.validate_replay_policy({"execution_policy": POLICY}, {"execution_policy": None})


def test_legacy_replay_requires_explicit_matching_image_id():
    image = {"image_id": "sha256:old", "execution_policy": None}
    replay.validate_replay_policy({}, image, legacy_image_id="sha256:old")
    for identity in (None, "sha256:other"):
        with pytest.raises(E2EError, match="legacy"):
            replay.validate_replay_policy({}, image, legacy_image_id=identity)
    with pytest.raises(E2EError, match="legacy"):
        replay.validate_replay_policy(
            {}, image | {"execution_policy": POLICY}, legacy_image_id="sha256:old"
        )


def test_incompatible_corpus_is_rejected_before_extraction_or_output_mutation(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    source = corpus / "manifest.json"
    source.write_text(json.dumps({"height": 1}))
    before = source.read_bytes()
    output = tmp_path / "output"
    with patch.object(
        replay, "inspect_replay_image", AsyncMock(return_value={"execution_policy": POLICY})
    ):
        with pytest.raises(E2EError, match="legacy"):
            asyncio.run(replay.verify_corpus(corpus, "new-image", output))
    assert not output.exists()
    assert source.read_bytes() == before


def test_previous_revision_corpus_requires_its_matching_image_policy():
    previous = POLICY | {"execution_revision": "xvm-execution-2"}
    # Historical replay stays possible with the pinned execution-2 image.
    replay.validate_replay_policy({"execution_policy": previous}, {"execution_policy": previous})
    for corpus, image in [(previous, POLICY), (POLICY, previous)]:
        with pytest.raises(E2EError, match="execution policy"):
            replay.validate_replay_policy({"execution_policy": corpus}, {"execution_policy": image})
