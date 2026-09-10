# Offline Palace installation on NCHC Forerunner 1

These scripts install the fixed CPU build of Palace source commit
`1ea14a84f60f672ffe4ce12dad404bc82896ef11` (tree
`246e6de4418c00488da225b99c3e9c3461e63beb`) below your F1 home directory.
F1 compute nodes have no external network access, so preparation downloads and verifies every
source on the login node. The build and simulations run only when you submit the supplied Slurm
scripts.

The fixed public Xmon bundle is owner-provided. It has no approved public download URL. Obtain the
exact archive through the authenticated custody channel and set `CASE_BUNDLE` to its local path.
The installer accepts only archive SHA-256
`c73772d1e8f739c3f174eecffb4a2b8bc5df7b2c622aa7302d4ea3adc09cdff6` and does not execute any
script from it.

## 1. Log in and prepare sources

Authenticate with your assigned F1 account and the current site OTP procedure. On the login node,
clone the public fork's `develop` branch explicitly; the repository's default branch may not contain
these scripts.

```bash
git clone --branch develop --single-branch https://github.com/arfiligol/palace.git palace-f1
cd palace-f1
git status --short --branch
git rev-parse HEAD
```

Use a clean checkout. `prepare.sh` records the exact script-delivery commit and hashes separately
from the fixed solver source commit. It creates the solver snapshot with `git archive`, then stages
the locked source archives, shallow bare repositories containing the exact commits, and CMake
binary kit. It does not configure or compile Palace because the home-built BLAS does not exist yet.
The login preparation requires Git, curl, and Python 3.6 or later; the helper avoids newer Python
site-library features so it remains compatible with the RHEL 8 environment.

```bash
export PALACE_F1_ROOT="$HOME/opt/palace"
read -r -p "Path to the custodian-provided Xmon archive: " CASE_BUNDLE
export CASE_BUNDLE
./scripts/f1/prepare.sh --root "$PALACE_F1_ROOT" --case-archive "$CASE_BUNDLE"
```

Preparation is restartable. Existing bytes are checksum-verified and reused; a conflicting cache or
modified source snapshot stops instead of being overwritten. If the case has not arrived, omit
`--case-archive`, then rerun the same command with it later.

## 2. Submit the compute build

Set the Slurm account assigned to you. Preparation already creates the log directory before Slurm
opens its output file.

```bash
read -r -p "F1 Slurm account: " F1_ACCOUNT
export F1_ACCOUNT
sbatch \
  --account="$F1_ACCOUNT" \
  --output="$PALACE_F1_ROOT/logs/build-%j.out" \
  --export=ALL,PALACE_F1_REPO="$PWD",PALACE_F1_ROOT="$PALACE_F1_ROOT" \
  scripts/f1/build.sbatch
```

The defaults are partition `ct112`, one node, one task with 8 CPUs, and an 8-hour limit. Build
parallelism comes only from `--cpus-per-task`; for example, add `--cpus-per-task=16` to the `sbatch`
command to use 16 compile jobs. The job checks x86-64, loads `gcc/11.2.0` followed by
`openmpi/4.1.6`, verifies all MPI compiler wrappers and Slurm PMIx support, then:

1. builds shared zlib 1.3.2, shared pthread OpenBLAS 0.3.20 LP64, and the locked FastFloat/scn pair
   into the final prefix;
2. configures the superbuild against those real libraries with every external URL remapped to the
   local cache and updates disconnected;
3. builds the locked CPU dependency matrix, static where supported (libCEED and LIBXSMM retain
   their upstream shared-library behavior and are captured by the runtime/ELF receipts);
4. configures the native Palace subproject with `PALACE_BUILD_EXTERNAL_DEPS=OFF`, avoiding the
   otherwise implicit Catch2 network fetch;
5. installs the direct ELF and libCEED JIT headers and writes checksum-bound build receipts.

The fixed install is
`$PALACE_F1_ROOT/versions/palace-1ea14a84-gcc11.2.0-openmpi4.1.6-openblas0.3.20-zlib1.3.2-cpu`.
An interrupted build can be resubmitted with the same command. A completed version is verified and
reused, never replaced. Changed toolchain or feature settings require a new version ID in a new
script delivery.

Check status and logs with normal Slurm commands:

```bash
squeue -u "$USER"
sacct -j JOB_ID --format=JobID,State,ExitCode,Elapsed,MaxRSS,AllocCPUS
tail -f "$PALACE_F1_ROOT/logs/build-JOB_ID.out"
```

Successful completion creates `BUILD-RECEIPT.json`, `BUILD-RECEIPT.md`,
`INSTALL-MANIFEST.sha256`, ELF dependency reports, and `run-env.sh` inside the immutable versioned
prefix. The resulting executable uses shared OpenBLAS, zlib, libCEED, and LIBXSMM together with the
GCC 11.2.0 and Open MPI 4.1.6 runtime supplied by the loaded F1 modules. This is build evidence; it
does not claim a solver result.

## 3. Submit the fixed comparison run

The default run requests one `ct112` node, 56 MPI ranks, one CPU/OpenMP thread per rank, and four
hours. It invokes the direct Palace ELF with `srun --mpi=pmix --cpu-bind=cores`; it does not call the
installed `palace` MPI wrapper.

```bash
sbatch \
  --account="$F1_ACCOUNT" \
  --output="$PALACE_F1_ROOT/logs/run-%j.out" \
  --export=ALL,PALACE_F1_REPO="$PWD",PALACE_F1_ROOT="$PALACE_F1_ROOT" \
  scripts/f1/run.sbatch
```

For 28 MPI ranks with two OpenMP threads each, change only the Slurm request:

```bash
sbatch \
  --account="$F1_ACCOUNT" --ntasks=28 --cpus-per-task=2 \
  --output="$PALACE_F1_ROOT/logs/run-%j.out" \
  --export=ALL,PALACE_F1_REPO="$PWD",PALACE_F1_ROOT="$PALACE_F1_ROOT" \
  scripts/f1/run.sbatch
```

`run.sbatch` derives ranks and threads from the allocation, fixes `OPENBLAS_NUM_THREADS=1`, and sets
`OMP_PROC_BIND=close` and `OMP_PLACES=cores`. A `ct112` node provides 112 CPU cores with roughly
4308 MB per allocated core; choose rank/thread counts and queue time within the current site policy.

Each job copies the verified input tree into
`$PALACE_F1_ROOT/runs/xmon-JOB_ID-rRESTART/work`, leaving the canonical case unchanged. The run
receipt records the exact command, source archive/config/mesh and executable hashes, exit code,
resource usage, complete work-file checksums, full solver log hash, and exact log lines concerning
frequencies, eigenvalues, AMR/refinement, iterations, and energy. A nonzero solver or `srun` result is
recorded as `PROCESS_FAILED` and returned to Slurm unchanged. An exit-zero process is
`PROCESS_COMPLETE`; both labels are execution evidence only, with semantic state `CONVERGING` and
scientific acceptance unset. `resource-usage.txt` is `/usr/bin/time` data for the local `srun`
launcher process, not aggregate per-rank resource usage. The receipt records that timing scope and
the combined stdout/stderr redirection to the full solver log.

The fixed input preserves FEM order 2 and AMR `Tol=0.02`, `MaxIts=20`, and `Fraction=0.15`. Its
historical mode-1 value of 4.645468307362 GHz came from completed iteration 18 of a process that later
exited 137; it is context for comparison, not a completed-run or scientific acceptance claim.

No script modifies shell profiles, logs into F1, transfers files, submits another job, generates or
rewrites the mesh/configuration, or accesses private NCUAS inputs.
