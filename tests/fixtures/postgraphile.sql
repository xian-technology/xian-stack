INSERT INTO blocks(height, block_hash, block_time, block_time_iso, tx_count, app_hash, created_at)
VALUES (1, 'block-1', 1767225600000000000, '2026-01-01Z', 1, 'app-1', '2026-01-01Z');
INSERT INTO transactions(hash, block_height, block_hash, block_time, tx_index, sender, nonce,
    contract, function, success, status_code, chi_used, result, payload, envelope, created_at)
VALUES ('tx-1', 1, 'block-1', 1767225600000000000, 0, 'alice', 1, 'currency', 'transfer',
    true, 0, 10, 'null', '{}', '{}', '2026-01-01Z');
INSERT INTO events(id, block_height, tx_hash, tx_index, event_index, contract, event,
    signer, caller, data_indexed, data, created_at) OVERRIDING SYSTEM VALUE
VALUES (9007199254740993, 1, 'tx-1', 0, 0, 'currency', 'Transfer', 'alice', 'alice',
    '{"to":"bob"}', '{"amount":{"__fixed__":"1.5"}}', '2026-01-01Z'),
    (9007199254740994, 1, 'tx-1', 0, 1, 'currency', 'Transfer', 'alice', 'alice',
    '{"to":"carol"}', '{"amount":{"__fixed__":"2.5"}}', '2026-01-01Z'),
    (9007199254740995, 1, 'tx-1', 0, 2, 'con_pairs', 'PairCreated', 'alice', 'alice',
    '{"token0":"con_a","token1":"currency"}', '{"pair":1}', '2026-01-01Z');
INSERT INTO state_changes(block_height, block_hash, block_time, tx_hash, tx_index, write_index,
    key, new_value, origin_type, created_at)
VALUES (1, 'block-1', 1767225600000000000, 'tx-1', 0, 0, 'currency.balances:bob',
    '{"__fixed__":"1.5"}', 'transaction', '2026-01-01Z');
INSERT INTO state(key, value, last_change_id, last_tx_hash, last_block_height, updated_at)
VALUES ('currency.balances:bob', '{"__fixed__":"1.5"}', 1, 'tx-1', 1, '2026-01-01Z');
INSERT INTO contracts(name, last_tx_hash, submitted_at_block, submitted_at, source, xsc001)
VALUES ('currency', 'tx-1', 1, '2026-01-01Z', 'source', true);
INSERT INTO rewards(block_height, tx_hash, tx_index, reward_index, type, recipient_key, value, created_at)
VALUES (1, 'tx-1', 0, 0, 'developer', 'alice', 2.5, '2026-01-01Z');

INSERT INTO state(key, value, last_change_id, last_tx_hash, last_block_height, updated_at)
VALUES ('currency.metadata:token_symbol', '"XIAN"', 1, 'tx-1', 1, '2026-01-01Z');
