from types import SimpleNamespace
from unittest.mock import patch

from test_localnet_e2e_expansion import localnet_e2e as e2e


def test_active_logs_and_rotation_keep_position_by_inode(tmp_path):
    node = SimpleNamespace(moniker="node-0")
    logs = tmp_path / ".localnet/node-0/.cometbft/xian/logs"
    logs.mkdir(parents=True)
    active = logs / "xian-abci.log"
    active.write_text("old marker\n")
    (logs / "other.log").write_text("unrelated")
    with patch.object(e2e, "STACK_DIR", tmp_path):
        paths = e2e.local_log_paths(node)
        assert paths == [active]
        positions = e2e.log_file_positions(paths)
        active.rename(logs / "xian-abci-123-1.log")
        active.write_text("new marker with a longer line\n")
        result = e2e.read_logs_since(e2e.local_log_paths(node), positions)
    assert "new marker" in result
    assert "old marker" not in result
    assert "unrelated" not in result
