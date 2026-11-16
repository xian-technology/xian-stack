// Package main verifies checkpoint consistency and live evidence material in a
// stopped, staged store. It does not change or replace upstream CometBFT.
package main

import (
	"bytes"
	"context"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"math/big"
	"os"
	"path/filepath"
	"reflect"
	"strconv"
	"time"

	dbm "github.com/cometbft/cometbft-db"
	cmtjson "github.com/cometbft/cometbft/libs/json"
	sm "github.com/cometbft/cometbft/state"
	"github.com/cometbft/cometbft/store"
	"github.com/cometbft/cometbft/types"
	"github.com/syndtr/goleveldb/leveldb/opt"
)

type options struct {
	MaxHistory  int64
	GenesisFile string
}
type evidenceReport struct {
	MaxAgeNumBlocks string `json:"max_age_num_blocks"`
	MaxAgeDuration  string `json:"max_age_duration"`
	MaxBytes        string `json:"max_bytes"`
}
type report struct {
	SchemaVersion        int            `json:"schema_version"`
	ChainID              string         `json:"chain_id"`
	Height               string         `json:"height"`
	InitialHeight        string         `json:"initial_height"`
	AppHash              string         `json:"app_hash"`
	BlockHash            string         `json:"block_hash"`
	LastBlockNanos       string         `json:"last_block_nanos"`
	Evidence             evidenceReport `json:"evidence"`
	BlockStoreBase       string         `json:"block_store_base"`
	BlockStoreHeight     string         `json:"block_store_height"`
	HistoryFirstVerified string         `json:"history_first_verified"`
	HistoryFirstNanos    string         `json:"history_first_nanos"`
}

type fastValidators interface {
	LoadValidatorsFast(int64) (*types.ValidatorSet, error)
}

func decimal(n int64) string { return strconv.FormatInt(n, 10) }
func nanos(t time.Time) *big.Int {
	n := new(big.Int).Mul(big.NewInt(t.Unix()), big.NewInt(1_000_000_000))
	return n.Add(n, big.NewInt(int64(t.Nanosecond())))
}
func expired(s sm.State, h int64, t time.Time) bool {
	elapsed := new(big.Int).Sub(nanos(s.LastBlockTime), nanos(t))
	return s.LastBlockHeight-h > s.ConsensusParams.Evidence.MaxAgeNumBlocks && elapsed.Cmp(big.NewInt(int64(s.ConsensusParams.Evidence.MaxAgeDuration))) > 0
}
func validateStateIdentity(s sm.State) error {
	if s.ChainID == "" || s.InitialHeight <= 0 || s.LastBlockHeight < s.InitialHeight {
		return errors.New("native checkpoint is missing")
	}
	if len(s.AppHash) != 32 {
		return errors.New("native checkpoint app hash must contain32bytes")
	}
	return nil
}
func validateBlockBounds(s sm.State, blocks *store.BlockStore) error {
	if blocks.Height() != s.LastBlockHeight || blocks.Base() < s.InitialHeight || blocks.Base() > blocks.Height() {
		return errors.New("native state and block-store heights disagree")
	}
	return nil
}
func validateEvidenceParams(p types.EvidenceParams) error {
	if p.MaxAgeNumBlocks <= 0 || p.MaxAgeDuration <= 0 || p.MaxBytes < 0 {
		return errors.New("invalid native evidence parameters")
	}
	return nil
}
func checkedState(db dbm.DB, blocks *store.BlockStore) (sm.State, error) {
	s, err := sm.NewStore(db, sm.StoreOptions{}).Load()
	if err != nil {
		return s, err
	}
	if err := validateStateIdentity(s); err != nil {
		return s, err
	}
	if err := validateBlockBounds(s, blocks); err != nil {
		return s, err
	}
	return s, validateEvidenceParams(s.ConsensusParams.Evidence)
}
func checkedHeader(blocks *store.BlockStore, s sm.State, h int64, higher *types.BlockMeta) (*types.BlockMeta, error) {
	meta := blocks.LoadBlockMeta(h)
	if meta == nil {
		return nil, fmt.Errorf("missing evidence header at height%d", h)
	}
	if err := validateMeta(meta); err != nil {
		return nil, fmt.Errorf("invalid header at height%d: %w", h, err)
	}
	if meta.Header.Height != h || meta.Header.ChainID != s.ChainID {
		return nil, fmt.Errorf("header identity mismatch at height%d", h)
	}
	if higher != nil {
		return linkedHeader(meta, higher)
	}
	if !s.LastBlockID.Equals(meta.BlockID) || !s.LastBlockTime.Equal(meta.Header.Time) {
		return nil, errors.New("native checkpoint block ID/time mismatch")
	}
	return meta, nil
}
func validateMeta(meta *types.BlockMeta) error {
	if !meta.BlockID.IsComplete() {
		return errors.New("incomplete block ID")
	}
	if err := meta.ValidateBasic(); err != nil {
		return err
	}
	return meta.Header.ValidateBasic()
}
func linkedHeader(meta, higher *types.BlockMeta) (*types.BlockMeta, error) {
	if !higher.Header.LastBlockID.Equals(meta.BlockID) {
		return nil, fmt.Errorf("broken header hash link at height%d", meta.Header.Height)
	}
	if higher.Header.Time.Before(meta.Header.Time) {
		return nil, errors.New("historical header time runs backwards")
	}
	return meta, nil
}
func checkedValidators(stateDB dbm.DB, meta *types.BlockMeta) (*types.ValidatorSet, error) {
	loader := sm.NewStore(stateDB, sm.StoreOptions{}).(fastValidators)
	vals, err := loader.LoadValidatorsFast(meta.Header.Height)
	if err != nil {
		return nil, err
	}
	if err := vals.ValidateBasic(); err != nil {
		return nil, err
	}
	if !bytes.Equal(vals.Hash(), meta.Header.ValidatorsHash) {
		return nil, fmt.Errorf("validator hash mismatch at height%d", meta.Header.Height)
	}
	return vals, nil
}
func loadCommitForConsumer(blocks *store.BlockStore, h int64, checkpoint bool) *types.Commit {
	if checkpoint {
		if seen := blocks.LoadSeenCommit(h); seen != nil {
			return seen
		}
	}
	return blocks.LoadBlockCommit(h)
}
func checkedCommit(blocks *store.BlockStore, meta, higher *types.BlockMeta) (*types.Commit, error) {
	h := meta.Header.Height
	commit := loadCommitForConsumer(blocks, h, higher == nil)
	if commit == nil {
		return nil, fmt.Errorf("missing evidence commit at height%d", h)
	}
	if err := commit.ValidateBasic(); err != nil {
		return nil, err
	}
	if commit.Height != h || !commit.BlockID.Equals(meta.BlockID) {
		return nil, fmt.Errorf("commit block ID/height mismatch at height%d", h)
	}
	if higher != nil && !bytes.Equal(commit.Hash(), higher.Header.LastCommitHash) {
		return nil, fmt.Errorf("commit hash mismatch at height%d", h)
	}
	return commit, nil
}
func checkpointValidatorLinks(stateDB dbm.DB, s sm.State, meta *types.BlockMeta) error {
	if s.LastValidators == nil || s.Validators == nil || s.NextValidators == nil {
		return errors.New("checkpoint validator sets are missing")
	}
	if !bytes.Equal(s.LastValidators.Hash(), meta.Header.ValidatorsHash) || !bytes.Equal(s.Validators.Hash(), meta.Header.NextValidatorsHash) {
		return errors.New("checkpoint validator sets differ from header")
	}
	loader := sm.NewStore(stateDB, sm.StoreOptions{}).(fastValidators)
	for h, expected := range map[int64]*types.ValidatorSet{s.LastBlockHeight + 1: s.Validators, s.LastBlockHeight + 2: s.NextValidators} {
		actual, err := loader.LoadValidatorsFast(h)
		if err != nil {
			return err
		}
		if !bytes.Equal(actual.Hash(), expected.Hash()) {
			return errors.New("checkpoint validator sets differ from native records")
		}
	}
	return nil
}
func checkLiveMaterial(stateDB dbm.DB, blocks *store.BlockStore, s sm.State, meta, higher *types.BlockMeta) error {
	vals, err := checkedValidators(stateDB, meta)
	if err != nil {
		return err
	}
	commit, err := checkedCommit(blocks, meta, higher)
	if err != nil {
		return err
	}
	if higher == nil {
		if err := checkpointValidatorLinks(stateDB, s, meta); err != nil {
			return err
		}
	}
	return vals.VerifyCommit(s.ChainID, meta.BlockID, meta.Header.Height, commit)
}
func scanHistory(ctx context.Context, stateDB dbm.DB, blocks *store.BlockStore, s sm.State, limit int64) (*types.BlockMeta, error) {
	var higher *types.BlockMeta
	for h := s.LastBlockHeight; h >= s.InitialHeight; h-- {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		if s.LastBlockHeight-h >= limit {
			return nil, errors.New("evidence history inspection exceeded height budget")
		}
		meta, err := checkedHeader(blocks, s, h, higher)
		if err != nil {
			return nil, err
		}
		if expired(s, h, meta.Header.Time) {
			return meta, nil
		}
		if err := checkLiveMaterial(stateDB, blocks, s, meta, higher); err != nil {
			return nil, err
		}
		if h == s.InitialHeight {
			return meta, nil
		}
		higher = meta
	}
	return nil, errors.New("no authenticated evidence history boundary")
}
func checkedBodyMeta(meta *types.BlockMeta) error {
	if meta.BlockSize <= 0 || meta.BlockSize > types.MaxBlockSizeBytes {
		return errors.New("retained block has invalid body size")
	}
	if meta.BlockID.PartSetHeader.Total > types.MaxBlockPartsCount {
		return errors.New("retained block has too many parts")
	}
	return nil
}
func checkedPart(blocks *store.BlockStore, meta *types.BlockMeta, index int) error {
	part := blocks.LoadBlockPart(meta.Header.Height, index)
	if part == nil {
		return fmt.Errorf("retained block%d is missing part%d", meta.Header.Height, index)
	}
	if err := part.ValidateBasic(); err != nil {
		return err
	}
	if part.Index != uint32(index) || part.Proof.Total != int64(meta.BlockID.PartSetHeader.Total) {
		return errors.New("retained block part identity mismatch")
	}
	return part.Proof.Verify(meta.BlockID.PartSetHeader.Hash, part.Bytes)
}
func checkedBody(blocks *store.BlockStore, meta *types.BlockMeta) error {
	if err := checkedBodyMeta(meta); err != nil {
		return err
	}
	for index := 0; index < int(meta.BlockID.PartSetHeader.Total); index++ {
		if err := checkedPart(blocks, meta, index); err != nil {
			return err
		}
	}
	block := blocks.LoadBlock(meta.Header.Height)
	if block == nil {
		return errors.New("retained block body is missing")
	}
	if err := block.ValidateBasic(); err != nil {
		return err
	}
	return checkedBodyIdentity(block, meta)
}
func checkedBodyIdentity(block *types.Block, meta *types.BlockMeta) error {
	parts, err := block.MakePartSet(types.BlockPartSizeBytes)
	if err != nil {
		return err
	}
	id := types.BlockID{Hash: block.Hash(), PartSetHeader: parts.Header()}
	if !id.Equals(meta.BlockID) || block.Size() != meta.BlockSize {
		return errors.New("retained block body hash/fullID/size mismatch")
	}
	return nil
}
func scanBodies(ctx context.Context, blocks *store.BlockStore, s sm.State, limit int64) error {
	var higher *types.BlockMeta
	for h := s.LastBlockHeight; h >= blocks.Base(); h-- {
		if err := ctx.Err(); err != nil {
			return err
		}
		if s.LastBlockHeight-h >= limit {
			return errors.New("retained body inspection exceeded height budget")
		}
		meta, err := checkedHeader(blocks, s, h, higher)
		if err != nil {
			return err
		}
		if err := checkedBody(blocks, meta); err != nil {
			return err
		}
		higher = meta
	}
	return nil
}
func makeReport(s sm.State, blocks *store.BlockStore, first *types.BlockMeta) *report {
	p := s.ConsensusParams.Evidence
	return &report{SchemaVersion: 1, ChainID: s.ChainID, Height: decimal(s.LastBlockHeight), InitialHeight: decimal(s.InitialHeight), AppHash: hex.EncodeToString(s.AppHash), BlockHash: hex.EncodeToString(s.LastBlockID.Hash), LastBlockNanos: nanos(s.LastBlockTime).String(), Evidence: evidenceReport{decimal(p.MaxAgeNumBlocks), decimal(int64(p.MaxAgeDuration)), decimal(p.MaxBytes)}, BlockStoreBase: decimal(blocks.Base()), BlockStoreHeight: decimal(blocks.Height()), HistoryFirstVerified: decimal(first.Header.Height), HistoryFirstNanos: nanos(first.Header.Time).String()}
}
func inspectStores(ctx context.Context, stateDB, blockDB dbm.DB, opts options) (result *report, err error) {
	defer func() {
		if p := recover(); p != nil {
			result = nil
			err = fmt.Errorf("corrupt native store: %v", p)
		}
	}()
	if opts.MaxHistory <= 0 {
		return nil, errors.New("history height budget must be positive")
	}
	blocks := store.NewBlockStore(blockDB)
	s, err := checkedState(stateDB, blocks)
	if err != nil {
		return nil, err
	}
	first, err := scanHistory(ctx, stateDB, blocks, s, opts.MaxHistory)
	if err != nil {
		return nil, err
	}
	if err := scanBodies(ctx, blocks, s, opts.MaxHistory); err != nil {
		return nil, err
	}
	return makeReport(s, blocks, first), nil
}
func requireStore(directory, name string) error {
	path := filepath.Join(directory, name+".db")
	info, err := os.Lstat(path)
	if err != nil {
		return err
	}
	if !info.IsDir() {
		return fmt.Errorf("native database must be an existing plain directory: %s", path)
	}
	lock, err := os.Lstat(filepath.Join(path, "LOCK"))
	if err != nil {
		return err
	}
	if !lock.Mode().IsRegular() {
		return errors.New("native database lock must be an existing regular file")
	}
	return nil
}
func openReadOnly(directory, name string) (dbm.DB, error) {
	if err := requireStore(directory, name); err != nil {
		return nil, err
	}
	return dbm.NewGoLevelDBWithOpts(name, directory, &opt.Options{ReadOnly: true, ErrorIfMissing: true, Strict: opt.StrictAll})
}
func readableEvidence(ctx context.Context, db dbm.DB, limit int64) error {
	it, err := db.Iterator(nil, nil)
	if err != nil {
		return err
	}
	defer it.Close()
	for count := int64(0); it.Valid(); count++ {
		if err := ctx.Err(); err != nil {
			return err
		}
		if count >= limit {
			return errors.New("evidence database inspection exceeded record budget")
		}
		_ = it.Value()
		it.Next()
	}
	return it.Error()
}
func normalizedGenesis(doc *types.GenesisDoc) (any, error) {
	encoded, err := cmtjson.Marshal(doc)
	if err != nil {
		return nil, err
	}
	var value any
	decoder := json.NewDecoder(bytes.NewReader(encoded))
	decoder.UseNumber()
	if err := decoder.Decode(&value); err != nil {
		return nil, err
	}
	return value, nil
}
func checkedCachedGenesis(stateDB dbm.DB, path string) (*types.GenesisDoc, error) {
	if path == "" {
		return nil, errors.New("trusted genesis-file is required")
	}
	expected, err := types.GenesisDocFromFile(path)
	if err != nil {
		return nil, fmt.Errorf("trusted genesis: %w", err)
	}
	raw, err := stateDB.Get([]byte("genesisDoc")) // official node/setup.go genesisDocKey
	if err != nil {
		return nil, err
	}
	if raw == nil {
		return nil, errors.New("native cached genesis is missing")
	}
	cached, err := types.GenesisDocFromJSON(raw)
	if err != nil {
		return nil, fmt.Errorf("native cached genesis: %w", err)
	}
	return compareGenesis(expected, cached)
}
func compareGenesis(expected, cached *types.GenesisDoc) (*types.GenesisDoc, error) {
	want, err := normalizedGenesis(expected)
	if err != nil {
		return nil, err
	}
	actual, err := normalizedGenesis(cached)
	if err != nil {
		return nil, err
	}
	if !reflect.DeepEqual(want, actual) {
		return nil, errors.New("native cached genesis differs from trusted genesis")
	}
	return expected, nil
}
func inspectWithGenesis(ctx context.Context, stateDB, blockDB dbm.DB, opts options) (*report, error) {
	genesis, err := checkedCachedGenesis(stateDB, opts.GenesisFile)
	if err != nil {
		return nil, err
	}
	result, err := inspectStores(ctx, stateDB, blockDB, opts)
	if err != nil {
		return nil, err
	}
	if result.ChainID != genesis.ChainID || result.InitialHeight != decimal(genesis.InitialHeight) {
		return nil, errors.New("native state genesis identity mismatch")
	}
	return result, nil
}
func withDirectory(ctx context.Context, directory, backend string, opts options, accept func(*report) error) (err error) {
	defer func() {
		if p := recover(); p != nil {
			err = fmt.Errorf("corrupt native store: %v", p)
		}
	}()
	if backend != "goleveldb" {
		return errors.New("verified full-store inspection supports only goleveldb")
	}
	sd, err := openReadOnly(directory, "state")
	if err != nil {
		return err
	}
	defer sd.Close()
	bd, err := openReadOnly(directory, "blockstore")
	if err != nil {
		return err
	}
	defer bd.Close()
	ed, err := openReadOnly(directory, "evidence")
	if err != nil {
		return err
	}
	defer ed.Close()
	if err := readableEvidence(ctx, ed, opts.MaxHistory); err != nil {
		return err
	}
	result, err := inspectWithGenesis(ctx, sd, bd, opts)
	if err != nil {
		return err
	}
	return accept(result)
}
func inspectDirectory(ctx context.Context, directory, backend string, opts options) (*report, error) {
	var result *report
	err := withDirectory(ctx, directory, backend, opts, func(r *report) error { result = r; return nil })
	return result, err
}
func waitForEOF(ctx context.Context, stdin io.Reader) error {
	done := make(chan error, 1)
	go func() {
		var one [1]byte
		_, err := io.ReadFull(stdin, one[:])
		if err == io.EOF {
			err = nil
		} else if err == nil {
			err = errors.New("hold-open accepts EOF only")
		}
		done <- err
	}()
	select {
	case err := <-done:
		return err
	case <-ctx.Done():
		return ctx.Err()
	}
}
func publishReport(ctx context.Context, stdout io.Writer, stdin io.Reader, hold bool, r *report) error {
	if err := json.NewEncoder(stdout).Encode(r); err != nil {
		return err
	}
	if hold {
		return waitForEOF(ctx, stdin)
	}
	return nil
}
func run(args []string, stdin io.Reader, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("xian-node-store-inspect", flag.ContinueOnError)
	fs.SetOutput(stderr)
	directory := fs.String("db-dir", "", "Stopped staged native database directory")
	genesis := fs.String("genesis-file", "", "Trusted local genesis file")
	backend := fs.String("db-backend", "goleveldb", "Native database backend")
	limit := fs.Int64("max-history-heights", 1_000_000, "Maximum headers/evidence records to inspect; raise explicitly if needed")
	hold := fs.Bool("hold-open", false, "Keep read-only store locks until stdin EOF or timeout")
	timeout := fs.Duration("timeout", 2*time.Minute, "Maximum inspection duration")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	if *directory == "" || *genesis == "" || fs.NArg() != 0 || *timeout <= 0 {
		fmt.Fprintln(stderr, "db-dir, positive timeout, and no positional arguments are required")
		return 2
	}
	ctx, cancel := context.WithTimeout(context.Background(), *timeout)
	defer cancel()
	err := withDirectory(ctx, *directory, *backend, options{MaxHistory: *limit, GenesisFile: *genesis}, func(r *report) error { return publishReport(ctx, stdout, stdin, *hold, r) })
	if err != nil {
		fmt.Fprintln(stderr, err)
		return 1
	}
	return 0
}
func main() { os.Exit(run(os.Args[1:], os.Stdin, os.Stdout, os.Stderr)) }
