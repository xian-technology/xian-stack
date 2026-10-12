// Live SDK transport and Python-to-JS encrypted-backup interoperability.
// Secrets arrive over stdin, never argv or process output.
import { readFileSync } from "node:fs";
import assert from "node:assert/strict";
import { XianClient, HybridAccountSigner, decryptAccountBackup } from "../../xian-js/packages/client/dist/index.js";
const input = JSON.parse(readFileSync(0, "utf8"));
const client = new XianClient({ rpcUrl: input.rpcUrl, chainId: input.chainId });
if (input.action === "absent") {
  assert.equal(await client.getState("_accounts", "records", [input.sender]), null);
} else {
  const signer = new HybridAccountSigner(await decryptAccountBackup(input.backup, input.password));
  const record = await client.getState("_accounts", "records", [input.sender]);
  assert.equal(record.epoch, 1);
  const tx = signer.signTransaction({ payload: input.payload });
  const result = await client.broadcastTx(tx, { waitForTx: true, timeoutMs: 30000 });
  assert.equal(result.accepted, true);
  assert.equal(result.finalized, true);
  assert.equal(result.receipt.success, true);
}
console.log(JSON.stringify({ passed: true, action: input.action }));
