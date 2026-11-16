package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"math/big"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	dbm "github.com/cometbft/cometbft-db"
	cmtjson "github.com/cometbft/cometbft/libs/json"
	cmtstore "github.com/cometbft/cometbft/proto/tendermint/store"
	cmtproto "github.com/cometbft/cometbft/proto/tendermint/types"
	sm "github.com/cometbft/cometbft/state"
	"github.com/cometbft/cometbft/store"
	"github.com/cometbft/cometbft/types"
)

type fixture struct {
	stateDB, blockDB dbm.DB
	state            sm.State
	blocks           *store.BlockStore
}

func must(t *testing.T, err error) {
	t.Helper()
	if err != nil {
		t.Fatal(err)
	}
}
func signedFixtureCommit(t *testing.T, signers map[string]types.MockPV, vals *types.ValidatorSet, id types.BlockID, h int64, chain string, now time.Time, corruptNil bool) *types.Commit {
	t.Helper()
	commit := &types.Commit{Height: h, Round: 0, BlockID: id, Signatures: make([]types.CommitSig, len(vals.Validators))}
	for index, val := range vals.Validators {
		pv := signers[string(val.Address)]
		voteID := id
		flag := types.BlockIDFlagCommit
		if len(vals.Validators) > 1 && index == len(vals.Validators)-1 {
			voteID = types.BlockID{}
			flag = types.BlockIDFlagNil
		}
		vote := &types.Vote{Type: cmtproto.PrecommitType, Height: h, Round: 0, BlockID: voteID, Timestamp: now, ValidatorAddress: val.Address, ValidatorIndex: int32(index)}
		proto := vote.ToProto()
		must(t, pv.SignVote(chain, proto))
		if flag == types.BlockIDFlagNil && corruptNil {
			proto.Signature[0] ^= 1
		}
		commit.Signatures[index] = types.CommitSig{BlockIDFlag: flag, ValidatorAddress: val.Address, Timestamp: now, Signature: proto.Signature}
	}
	return commit
}
func nativeFixture(t *testing.T, h int64) fixture { return fixtureWithNilVote(t, h, 1, 0) }
func fixtureWithNilVote(t *testing.T, h int64, validatorCount int, corruptAt int64) fixture {
	t.Helper()
	signers := map[string]types.MockPV{}
	validators := make([]types.GenesisValidator, validatorCount)
	for index := range validators {
		pv := types.NewMockPV()
		pub, err := pv.GetPubKey()
		must(t, err)
		signers[string(pub.Address())] = pv
		validators[index] = types.GenesisValidator{PubKey: pub, Power: 10}
	}
	params := types.DefaultConsensusParams()
	params.Evidence.MaxAgeNumBlocks = 2
	params.Evidence.MaxAgeDuration = 2 * time.Second
	genesis := &types.GenesisDoc{ChainID: "inspection-test", InitialHeight: 1, GenesisTime: time.Date(2026, 11, 16, 0, 0, 0, 0, time.UTC), ConsensusParams: params, Validators: validators, AppHash: bytes.Repeat([]byte{0}, 32)}
	s, err := sm.MakeGenesisState(genesis)
	must(t, err)
	sd, bd := dbm.NewMemDB(), dbm.NewMemDB()
	cached, err := cmtjson.Marshal(genesis)
	must(t, err)
	must(t, sd.SetSync([]byte("genesisDoc"), cached))
	ss := sm.NewStore(sd, sm.StoreOptions{})
	must(t, ss.Save(s))
	bs := store.NewBlockStore(bd)
	last := new(types.Commit)
	for n := int64(1); n <= h; n++ {
		block, err := s.MakeBlock(n, nil, last, nil, s.Validators.GetProposer().Address)
		must(t, err)
		block.Time = s.LastBlockTime.Add(time.Second)
		ps, err := block.MakePartSet(types.BlockPartSizeBytes)
		must(t, err)
		id := types.BlockID{Hash: block.Hash(), PartSetHeader: ps.Header()}
		commit := signedFixtureCommit(t, signers, s.Validators, id, n, s.ChainID, block.Time, n == corruptAt)
		bs.SaveBlock(block, ps, commit)
		s.LastBlockHeight = n
		s.LastBlockID = id
		s.LastBlockTime = block.Time
		s.LastValidators = s.Validators.Copy()
		s.Validators = s.NextValidators.Copy()
		s.NextValidators = s.Validators.Copy()
		s.AppHash = bytes.Repeat([]byte{byte(n)}, 32)
		must(t, ss.Save(s))
		last = commit
	}
	return fixture{sd, bd, s, bs}
}
func TestNativeCheckpointAndCompleteLiveHistory(t *testing.T) {
	f := nativeFixture(t, 6)
	r, err := inspectStores(context.Background(), f.stateDB, f.blockDB, options{MaxHistory: 100})
	must(t, err)
	if r == nil || r.Height != "6" {
		t.Fatalf("expected inspected checkpoint6, got %+v", r)
	}
}

func setState(t *testing.T, f fixture, change func(*sm.State)) {
	t.Helper()
	s := f.state
	change(&s)
	must(t, sm.NewStore(f.stateDB, sm.StoreOptions{}).Save(s))
}
func expectFailure(t *testing.T, f fixture, fragment string) {
	t.Helper()
	r, err := inspectStores(context.Background(), f.stateDB, f.blockDB, options{MaxHistory: 100})
	if err == nil || !strings.Contains(err.Error(), fragment) || r != nil {
		t.Fatalf("expected %q failure without result, got result%+v/error%v", fragment, r, err)
	}
}
func TestCheckpointReportUsesCommittedAppRootAndExpiredBoundary(t *testing.T) {
	f := nativeFixture(t, 6)
	r, err := inspectStores(context.Background(), f.stateDB, f.blockDB, options{MaxHistory: 100})
	must(t, err)
	if r.SchemaVersion != 1 || r.ChainID != "inspection-test" || r.InitialHeight != "1" || r.HistoryFirstVerified != "3" || r.BlockStoreBase != "1" || r.BlockStoreHeight != "6" {
		t.Fatalf("unexpected report %+v", r)
	}
	if r.AppHash != hex.EncodeToString(f.state.AppHash) || r.BlockHash != hex.EncodeToString(f.state.LastBlockID.Hash) || r.LastBlockNanos != nanos(f.state.LastBlockTime).String() || r.HistoryFirstNanos != nanos(f.state.LastBlockTime.Add(-3*time.Second)).String() {
		t.Fatalf("checkpoint root/time mismatch %+v", r)
	}
	if r.AppHash == hex.EncodeToString(f.blocks.LoadBlockMeta(6).Header.AppHash) {
		t.Fatal("used headerH previous-state apphash")
	}
	if r.Evidence != (evidenceReport{"2", "2000000000", "1048576"}) {
		t.Fatalf("wrong full evidence triple %+v", r.Evidence)
	}
}
func TestMissingLiveMaterialAndBoundaryFailClosed(t *testing.T) {
	for _, tc := range []struct{ name, key, fragment string }{{"header", "H:4", "missing evidence header"}, {"boundary", "H:3", "missing evidence header"}, {"commit", "C:4", "missing evidence commit"}, {"validators", "validatorsKey:4", "validator"}} {
		t.Run(tc.name, func(t *testing.T) {
			f := nativeFixture(t, 6)
			db := f.blockDB
			if tc.name == "validators" {
				db = f.stateDB
			}
			must(t, db.Delete([]byte(tc.key)))
			if tc.name == "commit" {
				must(t, db.Delete([]byte("SC:4")))
			}
			expectFailure(t, f, tc.fragment)
		})
	}
}
func TestExpiredBoundaryNeedsNoExpiredCommitOrValidators(t *testing.T) {
	f := nativeFixture(t, 6)
	must(t, f.blockDB.Delete([]byte("C:3")))
	must(t, f.stateDB.Delete([]byte("validatorsKey:3")))
	r, err := inspectStores(context.Background(), f.stateDB, f.blockDB, options{MaxHistory: 100})
	must(t, err)
	if r.HistoryFirstVerified != "3" {
		t.Fatal(r)
	}
}
func TestExpiryRequiresBothStrictAgeLimits(t *testing.T) {
	for _, tc := range []struct {
		name     string
		blocks   int64
		duration time.Duration
	}{{"duration stilllive", 2, 100 * time.Second}, {"blocks stilllive", 100, 2 * time.Second}, {"strict equality", 3, 3 * time.Second}} {
		t.Run(tc.name, func(t *testing.T) {
			f := nativeFixture(t, 6)
			setState(t, f, func(s *sm.State) {
				s.ConsensusParams.Evidence.MaxAgeNumBlocks = tc.blocks
				s.ConsensusParams.Evidence.MaxAgeDuration = tc.duration
			})
			must(t, f.stateDB.Delete([]byte("validatorsKey:3")))
			expectFailure(t, f, "validator")
		})
	}
}
func TestShortHistoryEndsAtInitialHeight(t *testing.T) {
	f := nativeFixture(t, 2)
	r, err := inspectStores(context.Background(), f.stateDB, f.blockDB, options{MaxHistory: 100})
	must(t, err)
	if r.HistoryFirstVerified != "1" {
		t.Fatal(r)
	}
}
func TestPrunedBodiesRetainNativeEvidenceMaterial(t *testing.T) {
	f := nativeFixture(t, 6)
	// Native pruning can retain live header/commit material below bodyBase.
	_, _, err := f.blocks.PruneBlocks(5, f.state)
	must(t, err)
	r, err := inspectStores(context.Background(), f.stateDB, f.blockDB, options{MaxHistory: 100})
	// This upstream pruner removes the expired boundary3, so its absence must fail.
	if err == nil || r != nil || !strings.Contains(err.Error(), "missing evidence header") {
		t.Fatalf("expected missing boundary, %+v %v", r, err)
	}
}
func TestCheckpointMismatchesFailClosed(t *testing.T) {
	for _, tc := range []struct {
		name     string
		change   func(*sm.State)
		fragment string
	}{
		{"height", func(s *sm.State) { s.LastBlockHeight-- }, "heights disagree"},
		{"time", func(s *sm.State) { s.LastBlockTime = s.LastBlockTime.Add(time.Second) }, "block ID/time mismatch"},
		{"blockID", func(s *sm.State) { s.LastBlockID.Hash = bytes.Repeat([]byte{99}, 32) }, "block ID/time mismatch"},
		{"appRoot", func(s *sm.State) { s.AppHash = nil }, "app hash"},
		{"nochain", func(s *sm.State) { s.ChainID = "" }, "checkpoint is missing"},
		{"evidence", func(s *sm.State) { s.ConsensusParams.Evidence.MaxAgeNumBlocks = 0 }, "evidence parameters"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			f := nativeFixture(t, 6)
			setState(t, f, tc.change)
			expectFailure(t, f, tc.fragment)
		})
	}
}
func TestChangedHistoricalCommitIsDetected(t *testing.T) {
	f := nativeFixture(t, 6)
	commit := f.blocks.LoadBlockCommit(4)
	commit.Signatures[0].Signature[0] ^= 1
	data, err := commit.ToProto().Marshal()
	must(t, err)
	must(t, f.blockDB.Set([]byte("C:4"), data))
	expectFailure(t, f, "commit hash mismatch")
}
func TestBadCheckpointSignatureIsRejected(t *testing.T) {
	f := nativeFixture(t, 6)
	commit := f.blocks.LoadSeenCommit(6)
	commit.Signatures[0].Signature[0] ^= 1
	data, err := commit.ToProto().Marshal()
	must(t, err)
	must(t, f.blockDB.Set([]byte("SC:6"), data))
	expectFailure(t, f, "signature")
}
func TestCorruptNativeRecordReturnsErrorWithoutResult(t *testing.T) {
	f := nativeFixture(t, 6)
	must(t, f.blockDB.Set([]byte("H:4"), []byte{0xff}))
	expectFailure(t, f, "corrupt native store")
}
func TestInspectionBudgetsAndCancellation(t *testing.T) {
	f := nativeFixture(t, 6)
	for _, limit := range []int64{0, 3} {
		r, err := inspectStores(context.Background(), f.stateDB, f.blockDB, options{MaxHistory: limit})
		if err == nil || r != nil {
			t.Fatalf("budget%d accepted", limit)
		}
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	r, err := inspectStores(ctx, f.stateDB, f.blockDB, options{MaxHistory: 100})
	if err != context.Canceled || r != nil {
		t.Fatalf("cancellation: %+v %v", r, err)
	}
}
func TestExactNanosecondsAndLargeTimestampExpiry(t *testing.T) {
	for _, year := range []int{1, 2500, 9999} {
		when := time.Date(year, 1, 2, 3, 4, 5, 123456789, time.UTC)
		expected := new(big.Int).Mul(big.NewInt(when.Unix()), big.NewInt(1_000_000_000))
		expected.Add(expected, big.NewInt(123456789))
		if nanos(when).Cmp(expected) != 0 {
			t.Fatal(year, nanos(when), expected)
		}
	}
	f := nativeFixture(t, 6)
	f.state.LastBlockTime = time.Date(9999, 1, 1, 0, 0, 0, 0, time.UTC)
	f.state.ConsensusParams.Evidence.MaxAgeDuration = time.Duration(1<<63 - 1)
	if !expired(f.state, 1, time.Date(1, 1, 1, 0, 0, 0, 0, time.UTC)) {
		t.Fatal("large timestamp subtraction overflowed")
	}
}
func persistDB(t *testing.T, source dbm.DB, name, directory string) {
	t.Helper()
	db, err := dbm.NewGoLevelDB(name, directory)
	must(t, err)
	defer db.Close()
	it, err := source.Iterator(nil, nil)
	must(t, err)
	defer it.Close()
	for ; it.Valid(); it.Next() {
		must(t, db.SetSync(it.Key(), it.Value()))
	}
	must(t, it.Error())
}
func diskFixture(t *testing.T) string {
	t.Helper()
	f := nativeFixture(t, 6)
	dir := t.TempDir()
	persistDB(t, f.stateDB, "state", dir)
	persistDB(t, f.blockDB, "blockstore", dir)
	persistDB(t, dbm.NewMemDB(), "evidence", dir)
	cached, err := f.stateDB.Get([]byte("genesisDoc"))
	must(t, err)
	must(t, os.WriteFile(filepath.Join(dir, "genesis.json"), cached, 0600))
	return dir
}
func treeHashes(t *testing.T, root string) map[string]string {
	t.Helper()
	result := map[string]string{}
	must(t, filepath.WalkDir(root, func(path string, entry os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if entry.Type().IsRegular() {
			data, err := os.ReadFile(path)
			if err != nil {
				return err
			}
			sum := sha256.Sum256(data)
			result[path] = hex.EncodeToString(sum[:])
		}
		return nil
	}))
	return result
}
func TestReadOnlyInspectionNeverChangesNativeFiles(t *testing.T) {
	dir := diskFixture(t)
	before := treeHashes(t, dir)
	r, err := inspectDirectory(context.Background(), dir, "goleveldb", options{MaxHistory: 100, GenesisFile: filepath.Join(dir, "genesis.json")})
	must(t, err)
	if r.Height != "6" {
		t.Fatal(r)
	}
	if !reflect.DeepEqual(before, treeHashes(t, dir)) {
		t.Fatal("read-only inspection changed native files")
	}
}
func TestMissingOrLockedStoresAreNeverCreatedOrRepaired(t *testing.T) {
	t.Run("missing", func(t *testing.T) {
		dir := t.TempDir()
		_, err := inspectDirectory(context.Background(), dir, "goleveldb", options{MaxHistory: 100, GenesisFile: filepath.Join(dir, "genesis.json")})
		if err == nil {
			t.Fatal("missing store accepted")
		}
		entries, err := os.ReadDir(dir)
		must(t, err)
		if len(entries) != 0 {
			t.Fatal("created missing stores")
		}
	})
	t.Run("missingLOCK", func(t *testing.T) {
		dir := diskFixture(t)
		must(t, os.Remove(filepath.Join(dir, "evidence.db/LOCK")))
		before := treeHashes(t, dir)
		_, err := inspectDirectory(context.Background(), dir, "goleveldb", options{MaxHistory: 100, GenesisFile: filepath.Join(dir, "genesis.json")})
		if err == nil {
			t.Fatal("missingLOCK accepted")
		}
		if !reflect.DeepEqual(before, treeHashes(t, dir)) {
			t.Fatal("createdLOCK")
		}
	})
	t.Run("activewriter", func(t *testing.T) {
		dir := diskFixture(t)
		db, err := dbm.NewGoLevelDB("blockstore", dir)
		must(t, err)
		defer db.Close()
		_, err = inspectDirectory(context.Background(), dir, "goleveldb", options{MaxHistory: 100, GenesisFile: filepath.Join(dir, "genesis.json")})
		if err == nil {
			t.Fatal("active native writer accepted")
		}
	})
	t.Run("backend", func(t *testing.T) {
		_, err := inspectDirectory(context.Background(), t.TempDir(), "rocksdb", options{MaxHistory: 100})
		if err == nil || !strings.Contains(err.Error(), "only goleveldb") {
			t.Fatal(err)
		}
	})
}
func TestInspectorChild(t *testing.T) {
	if os.Getenv("XIAN_INSPECTOR_TEST_CHILD") != "1" {
		return
	}
	for index, arg := range os.Args {
		if arg == "--" {
			os.Exit(run(os.Args[index+1:], os.Stdin, os.Stdout, os.Stderr))
		}
	}
	os.Exit(3)
}
func TestHoldOpenKeepsNativeLocksUntilEOF(t *testing.T) {
	dir := diskFixture(t)
	exe, err := os.Executable()
	must(t, err)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	process := exec.CommandContext(ctx, exe, "-test.run=TestInspectorChild", "--", "--db-dir", dir, "--genesis-file", filepath.Join(dir, "genesis.json"), "--hold-open")
	process.Env = append(os.Environ(), "XIAN_INSPECTOR_TEST_CHILD=1")
	stdin, err := process.StdinPipe()
	must(t, err)
	stdout, err := process.StdoutPipe()
	must(t, err)
	var stderr bytes.Buffer
	process.Stderr = &stderr
	must(t, process.Start())
	defer process.Process.Kill()
	var r report
	must(t, json.NewDecoder(stdout).Decode(&r))
	if r.Height != "6" {
		t.Fatal(r)
	}
	db, err := dbm.NewGoLevelDB("state", dir)
	if err == nil {
		db.Close()
		t.Fatal("writer opened during lease")
	}
	must(t, stdin.Close())
	must(t, process.Wait())
	if stderr.Len() != 0 {
		t.Fatal(stderr.String())
	}
	db, err = dbm.NewGoLevelDB("state", dir)
	must(t, err)
	must(t, db.Close())
}
func TestLeaseTimeoutAndNonEOFInputFailClosed(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	read, write := io.Pipe()
	defer read.Close()
	defer write.Close()
	if err := waitForEOF(ctx, read); err != context.Canceled {
		t.Fatal(err)
	}
	if err := waitForEOF(context.Background(), strings.NewReader("command")); err == nil {
		t.Fatal("lease control accepted input")
	}
}

func TestNativeMetadataBelowBodyBaseCanPass(t *testing.T) {
	f := nativeFixture(t, 6)
	// Keep the authenticated expired boundary+live metadata, while bodies1..4
	// are unavailable. Body pruning cannot substitute for the metadata scan.
	for h := int64(1); h < 5; h++ {
		must(t, f.blockDB.Delete([]byte(fmt.Sprintf("P:%d:0", h))))
	}
	store.SaveBlockStoreState(&cmtstore.BlockStoreState{Base: 5, Height: 6}, f.blockDB)
	r, err := inspectStores(context.Background(), f.stateDB, f.blockDB, options{MaxHistory: 100})
	must(t, err)
	if r.BlockStoreBase != "5" || r.HistoryFirstVerified != "3" {
		t.Fatal(r)
	}
	if store.NewBlockStore(f.blockDB).LoadBlock(4) != nil {
		t.Fatal("expected unavailable old body")
	}
}

func TestCheckpointValidatorStateMustMatchStoredHeadersAndRecords(t *testing.T) {
	for _, which := range []string{"last", "current", "next"} {
		t.Run(which, func(t *testing.T) {
			f := nativeFixture(t, 6)
			setState(t, f, func(s *sm.State) {
				switch which {
				case "last":
					s.LastValidators = s.LastValidators.Copy()
					s.LastValidators.Validators[0].VotingPower++
				case "current":
					s.Validators = s.Validators.Copy()
					s.Validators.Validators[0].VotingPower++
				case "next":
					s.NextValidators = s.NextValidators.Copy()
					s.NextValidators.Validators[0].VotingPower++
				}
			})
			expectFailure(t, f, "validator")
		})
	}
}

func TestCommandProducesStrictJSONAndNoProofOnError(t *testing.T) {
	dir := diskFixture(t)
	var out, errOut bytes.Buffer
	if code := run([]string{"--db-dir", dir, "--genesis-file", filepath.Join(dir, "genesis.json")}, strings.NewReader(""), &out, &errOut); code != 0 {
		t.Fatal(code, errOut.String())
	}
	var decoded map[string]any
	must(t, json.Unmarshal(out.Bytes(), &decoded))
	if len(decoded) != 12 || decoded["schema_version"] != float64(1) || decoded["height"] != "6" {
		t.Fatal(decoded)
	}
	for _, args := range [][]string{{}, {"--unknown"}, {"--db-dir", dir, "extra"}, {"--db-dir", dir, "--timeout", "0s"}, {"--db-dir", dir, "--db-backend", "rocksdb"}, {"--db-dir", dir, "--max-history-heights", "0"}} {
		out.Reset()
		errOut.Reset()
		if code := run(args, strings.NewReader(""), &out, &errOut); code == 0 || out.Len() != 0 || errOut.Len() == 0 {
			t.Fatalf("bad args%v: code%d out%s err%s", args, code, out.String(), errOut.String())
		}
	}
}

type brokenWriter struct{}

func (brokenWriter) Write([]byte) (int, error) { return 0, io.ErrClosedPipe }
func TestOutputFailureReleasesAllReadOnlyLocks(t *testing.T) {
	dir := diskFixture(t)
	var errOut bytes.Buffer
	if code := run([]string{"--db-dir", dir, "--genesis-file", filepath.Join(dir, "genesis.json")}, strings.NewReader(""), brokenWriter{}, &errOut); code == 0 || !strings.Contains(errOut.String(), "closed pipe") {
		t.Fatal(code, errOut.String())
	}
	for _, name := range []string{"state", "blockstore", "evidence"} {
		db, err := dbm.NewGoLevelDB(name, dir)
		must(t, err)
		must(t, db.Close())
	}
}
func TestEvidenceDatabaseRecordBudget(t *testing.T) {
	db := dbm.NewMemDB()
	must(t, db.Set([]byte("a"), []byte("1")))
	must(t, db.Set([]byte("b"), []byte("2")))
	if err := readableEvidence(context.Background(), db, 1); err == nil || !strings.Contains(err.Error(), "record budget") {
		t.Fatal(err)
	}
	must(t, readableEvidence(context.Background(), db, 2))
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := readableEvidence(ctx, db, 2); err != context.Canceled {
		t.Fatal(err)
	}
}
func TestReadOnlyPathsMustBePlainExistingStoreAndLock(t *testing.T) {
	for _, which := range []string{"dbfile", "dbsymlink", "lockdir", "locksymlink"} {
		t.Run(which, func(t *testing.T) {
			dir := t.TempDir()
			path := filepath.Join(dir, "state.db")
			switch which {
			case "dbfile":
				must(t, os.WriteFile(path, []byte("file"), 0600))
			case "dbsymlink":
				target := t.TempDir()
				must(t, os.Symlink(target, path))
			default:
				must(t, os.Mkdir(path, 0700))
				if which == "lockdir" {
					must(t, os.Mkdir(filepath.Join(path, "LOCK"), 0700))
				} else {
					target := filepath.Join(dir, "target")
					must(t, os.WriteFile(target, nil, 0600))
					must(t, os.Symlink(target, filepath.Join(path, "LOCK")))
				}
			}
			if _, err := openReadOnly(dir, "state"); err == nil {
				t.Fatal("nonplain native store accepted")
			}
		})
	}
}
func TestBoundsParametersAndIdentityChecks(t *testing.T) {
	f := nativeFixture(t, 6)
	for _, base := range []int64{-1, 7} {
		store.SaveBlockStoreState(&cmtstore.BlockStoreState{Base: base, Height: 6}, f.blockDB)
		if err := validateBlockBounds(f.state, store.NewBlockStore(f.blockDB)); err == nil {
			t.Fatal(base)
		}
	}
	for _, change := range []func(*types.EvidenceParams){func(p *types.EvidenceParams) { p.MaxAgeNumBlocks = 0 }, func(p *types.EvidenceParams) { p.MaxAgeDuration = 0 }, func(p *types.EvidenceParams) { p.MaxBytes = -1 }} {
		p := f.state.ConsensusParams.Evidence
		change(&p)
		if err := validateEvidenceParams(p); err == nil {
			t.Fatal(p)
		}
	}
	s := f.state
	s.InitialHeight = 0
	if err := validateStateIdentity(s); err == nil {
		t.Fatal("zero initialheight accepted")
	}
}

func TestMissingRetainedBlockPartIsRejected(t *testing.T) {
	f := nativeFixture(t, 6)
	must(t, f.blockDB.Delete([]byte("P:2:0")))
	expectFailure(t, f, "block")
}
func TestHistoricalCommitRoundIsAuthenticated(t *testing.T) {
	f := nativeFixture(t, 6)
	commit := f.blocks.LoadBlockCommit(4)
	before := append([]byte(nil), commit.Hash()...)
	commit.Round++
	if !bytes.Equal(before, commit.Hash()) {
		t.Fatal("fixture requires upstreamCommit.Hash omitsRound")
	}
	data, err := commit.ToProto().Marshal()
	must(t, err)
	must(t, f.blockDB.Set([]byte("C:4"), data))
	expectFailure(t, f, "signature")
}
func TestExpiryStrictTruthTable(t *testing.T) {
	f := nativeFixture(t, 6)
	for _, tc := range []struct {
		height  int64
		seconds int
		want    bool
	}{{5, 3, false}, {4, 3, false}, {3, 2, false}, {3, 3, true}, {3, 1, false}, {6, 10, false}} {
		got := expired(f.state, tc.height, f.state.LastBlockTime.Add(-time.Duration(tc.seconds)*time.Second))
		if got != tc.want {
			t.Fatalf("height%d age%ds: got%t want%t", tc.height, tc.seconds, got, tc.want)
		}
	}
}
func TestMinimumValidCheckpointAndParameters(t *testing.T) {
	f := nativeFixture(t, 1)
	r, err := inspectStores(context.Background(), f.stateDB, f.blockDB, options{MaxHistory: 1})
	must(t, err)
	if r.Height != "1" || r.HistoryFirstVerified != "1" {
		t.Fatal(r)
	}
	p := types.EvidenceParams{MaxAgeNumBlocks: 1, MaxAgeDuration: time.Nanosecond, MaxBytes: 0}
	must(t, validateEvidenceParams(p))
	f = nativeFixture(t, 6)
	boundary, err := scanHistory(context.Background(), f.stateDB, store.NewBlockStore(f.blockDB), f.state, 4)
	must(t, err)
	if boundary.Header.Height != 3 {
		t.Fatal(boundary)
	}
}
func TestHistoricalCommitHeightAndFullIDMismatch(t *testing.T) {
	for _, which := range []string{"height", "partset"} {
		t.Run(which, func(t *testing.T) {
			f := nativeFixture(t, 6)
			commit := f.blocks.LoadBlockCommit(4)
			if which == "height" {
				commit.Height++
			} else {
				commit.BlockID.PartSetHeader.Total++
			}
			data, err := commit.ToProto().Marshal()
			must(t, err)
			must(t, f.blockDB.Set([]byte("C:4"), data))
			_, err = checkedCommit(store.NewBlockStore(f.blockDB), f.blocks.LoadBlockMeta(4), f.blocks.LoadBlockMeta(5))
			if err == nil || !strings.Contains(err.Error(), "block ID/height mismatch") {
				t.Fatal(err)
			}
		})
	}
}

func TestCachedNativeGenesisMustMatchTrustedReference(t *testing.T) {
	for _, which := range []string{"missing", "corrupt", "apphash", "validators", "params", "time", "chain"} {
		t.Run(which, func(t *testing.T) {
			dir := diskFixture(t)
			db, err := dbm.NewGoLevelDB("state", dir)
			must(t, err)
			raw, err := db.Get([]byte("genesisDoc"))
			must(t, err)
			switch which {
			case "missing":
				must(t, db.DeleteSync([]byte("genesisDoc")))
			case "corrupt":
				must(t, db.SetSync([]byte("genesisDoc"), []byte("badJSON")))
			default:
				doc, err := types.GenesisDocFromJSON(raw)
				must(t, err)
				switch which {
				case "apphash":
					doc.AppHash = bytes.Repeat([]byte{99}, 32)
				case "validators":
					doc.Validators[0].Power++
				case "params":
					doc.ConsensusParams.Evidence.MaxAgeDuration += time.Second
				case "time":
					doc.GenesisTime = doc.GenesisTime.Add(time.Second)
				case "chain":
					doc.ChainID = "other-chain"
				}
				data, err := cmtjson.Marshal(doc)
				must(t, err)
				must(t, db.SetSync([]byte("genesisDoc"), data))
			}
			must(t, db.Close())
			r, err := inspectDirectory(context.Background(), dir, "goleveldb", options{MaxHistory: 100, GenesisFile: filepath.Join(dir, "genesis.json")})
			if err == nil || r != nil {
				t.Fatalf("cachedgenesis%s accepted %+v %v", which, r, err)
			}
		})
	}
}

func rewriteMeta(t *testing.T, f fixture, h int64, change func(*types.BlockMeta)) {
	t.Helper()
	meta := store.NewBlockStore(f.blockDB).LoadBlockMeta(h)
	change(meta)
	data, err := meta.ToProto().Marshal()
	must(t, err)
	must(t, f.blockDB.Set([]byte(fmt.Sprintf("H:%d", h)), data))
}
func TestRetainedBodySizeAndPartProofCorruptionFailClosed(t *testing.T) {
	for _, which := range []string{"size", "proof", "data", "checkpointmissing"} {
		t.Run(which, func(t *testing.T) {
			f := nativeFixture(t, 6)
			switch which {
			case "size":
				rewriteMeta(t, f, 2, func(meta *types.BlockMeta) { meta.BlockSize++ })
			case "checkpointmissing":
				must(t, f.blockDB.Delete([]byte("P:6:0")))
			default:
				part := f.blocks.LoadBlockPart(2, 0)
				if which == "proof" {
					part.Proof.LeafHash[0] ^= 1
				} else {
					part.Bytes[0] ^= 1
				}
				proto, err := part.ToProto()
				must(t, err)
				data, err := proto.Marshal()
				must(t, err)
				must(t, f.blockDB.Set([]byte("P:2:0"), data))
			}
			r, err := inspectStores(context.Background(), f.stateDB, f.blockDB, options{MaxHistory: 100})
			if err == nil || r != nil {
				t.Fatalf("body%s accepted %+v %v", which, r, err)
			}
		})
	}
}
func TestRetainedBodyFullIDAndResourceBounds(t *testing.T) {
	f := nativeFixture(t, 6)
	meta := f.blocks.LoadBlockMeta(2)
	meta.BlockID.PartSetHeader.Hash[0] ^= 1
	if err := checkedBody(store.NewBlockStore(f.blockDB), meta); err == nil {
		t.Fatal("bodyfullID mismatch accepted")
	}
	for _, which := range []string{"zero", "oversize", "parts"} {
		meta = f.blocks.LoadBlockMeta(3)
		switch which {
		case "zero":
			meta.BlockSize = 0
		case "oversize":
			meta.BlockSize = types.MaxBlockSizeBytes + 1
		case "parts":
			meta.BlockID.PartSetHeader.Total = types.MaxBlockPartsCount + 1
		}
		if err := checkedBodyMeta(meta); err == nil {
			t.Fatal(which)
		}
	}
}
func TestTrustedGenesisRequiredAndSemanticNormalization(t *testing.T) {
	dir := diskFixture(t)
	opts := options{MaxHistory: 100, GenesisFile: filepath.Join(dir, "genesis.json")}
	raw, err := os.ReadFile(opts.GenesisFile)
	must(t, err)
	var generic any
	must(t, json.Unmarshal(raw, &generic))
	pretty, err := json.MarshalIndent(generic, "", "  ")
	must(t, err)
	must(t, os.WriteFile(opts.GenesisFile, pretty, 0600))
	_, err = inspectDirectory(context.Background(), dir, "goleveldb", opts)
	must(t, err)
	_, err = inspectDirectory(context.Background(), dir, "goleveldb", options{MaxHistory: 100})
	if err == nil || !strings.Contains(err.Error(), "genesis-file") {
		t.Fatal(err)
	}
	var out, errOut bytes.Buffer
	if code := run([]string{"--db-dir", dir}, strings.NewReader(""), &out, &errOut); code != 2 || out.Len() != 0 {
		t.Fatal(code, out.String())
	}
}

func TestResourceValidatorsAcceptLegalDeclaredBoundaries(t *testing.T) {
	f := nativeFixture(t, 6)
	for _, size := range []int{1, types.MaxBlockSizeBytes} {
		meta := f.blocks.LoadBlockMeta(2)
		meta.BlockSize = size
		must(t, checkedBodyMeta(meta))
	}
	meta := f.blocks.LoadBlockMeta(3)
	meta.BlockID.PartSetHeader.Total = types.MaxBlockPartsCount
	must(t, checkedBodyMeta(meta))
}
func TestIndependentScanBudgetsRejectOneRecordBeyondLimit(t *testing.T) {
	f := nativeFixture(t, 6)
	if _, err := scanHistory(context.Background(), f.stateDB, store.NewBlockStore(f.blockDB), f.state, 3); err == nil {
		t.Fatal("history readbeyondbudget")
	}
	if err := scanBodies(context.Background(), store.NewBlockStore(f.blockDB), f.state, 5); err == nil {
		t.Fatal("bodyreadbeyondbudget")
	}
	must(t, f.blockDB.Delete([]byte("P:1:0")))
	expectFailure(t, f, "block")
}
func TestPartTotalMustMatchNativeBlockID(t *testing.T) {
	f := nativeFixture(t, 6)
	meta := f.blocks.LoadBlockMeta(2)
	meta.BlockID.PartSetHeader.Total++
	if err := checkedPart(store.NewBlockStore(f.blockDB), meta, 0); err == nil {
		t.Fatal("validpartproof withwrong totalaccepted")
	}
}
func TestExistingDatabaseWithSymlinkLockIsRejected(t *testing.T) {
	dir := diskFixture(t)
	path := filepath.Join(dir, "state.db/LOCK")
	must(t, os.Rename(path, path+".original"))
	must(t, os.Symlink("LOCK.original", path))
	if db, err := openReadOnly(dir, "state"); err == nil {
		db.Close()
		t.Fatal("validdatabasewithsymlinkLOCK accepted")
	}
}

func TestValidNILVotesRemainSupported(t *testing.T) {
	f := fixtureWithNilVote(t, 6, 4, 0)
	r, err := inspectStores(context.Background(), f.stateDB, f.blockDB, options{MaxHistory: 100})
	must(t, err)
	if r.Height != "6" {
		t.Fatal(r)
	}
}
func TestInvalidNILSignatureCannotReachConsensusRestart(t *testing.T) {
	f := fixtureWithNilVote(t, 6, 4, 6)
	commit := f.blocks.LoadSeenCommit(6)
	vals, err := sm.NewStore(f.stateDB, sm.StoreOptions{}).(fastValidators).LoadValidatorsFast(6)
	must(t, err)
	must(t, vals.VerifyCommitLightAllSignatures(f.state.ChainID, commit.BlockID, 6, commit))
	if err := vals.VerifyCommit(f.state.ChainID, commit.BlockID, 6, commit); err == nil {
		t.Fatal("fixture requires strict native rejection ofNIL signature")
	}
	panicked := false
	func() { defer func() { panicked = recover() != nil }(); commit.ToVoteSet(f.state.ChainID, vals) }()
	if !panicked {
		t.Fatal("fixture requires actualrestart vote-reconstruction panic")
	}
	expectFailure(t, f, "signature")
}
func TestInvalidHistoricalNILSignatureIsRejected(t *testing.T) {
	f := fixtureWithNilVote(t, 6, 4, 4)
	expectFailure(t, f, "signature")
}
func TestCheckpointPrefersActualSeenCommitRestartConsumer(t *testing.T) {
	f := nativeFixture(t, 6)
	good := f.blocks.LoadSeenCommit(6)
	encoded, err := good.ToProto().Marshal()
	must(t, err)
	must(t, f.blockDB.Set([]byte("C:6"), encoded))
	good.Signatures[0].Signature[0] ^= 1
	bad, err := good.ToProto().Marshal()
	must(t, err)
	must(t, f.blockDB.Set([]byte("SC:6"), bad))
	expectFailure(t, f, "signature")
}
func TestHistoricalCanonicalCommitCannotUseSeenFallback(t *testing.T) {
	f := nativeFixture(t, 6)
	must(t, f.blockDB.Delete([]byte("C:4")))
	if store.NewBlockStore(f.blockDB).LoadSeenCommit(4) == nil {
		t.Fatal("fixture needs matchingSeenCommit")
	}
	expectFailure(t, f, "missing evidence commit")
}

func TestCheckpointCanUseCanonicalWhenSeenIsMissing(t *testing.T) {
	f := nativeFixture(t, 6)
	good := f.blocks.LoadSeenCommit(6)
	data, err := good.ToProto().Marshal()
	must(t, err)
	must(t, f.blockDB.Set([]byte("C:6"), data))
	must(t, f.blockDB.Delete([]byte("SC:6")))
	_, err = inspectStores(context.Background(), f.stateDB, f.blockDB, options{MaxHistory: 100})
	must(t, err)
}
func TestHistoricalConsumerIgnoresUnselectedSeenCommit(t *testing.T) {
	f := nativeFixture(t, 6)
	seen := f.blocks.LoadSeenCommit(4)
	seen.Signatures[0].Signature[0] ^= 1
	data, err := seen.ToProto().Marshal()
	must(t, err)
	must(t, f.blockDB.Set([]byte("SC:4"), data))
	_, err = inspectStores(context.Background(), f.stateDB, f.blockDB, options{MaxHistory: 100})
	must(t, err)
}
