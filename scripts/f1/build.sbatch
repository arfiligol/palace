#!/usr/bin/env bash
#SBATCH --job-name=palace-f1-build
#SBATCH --partition=ct112
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --time=08:00:00

# Build the locked CPU installation on an F1 compute node from the login-prepared cache.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: sbatch [resource options] scripts/f1/build.sbatch [--root PATH]

Build parallelism is taken only from Slurm's --cpus-per-task value.
EOF
}

if [[ -n ${PALACE_F1_REPO:-} ]]; then
  repo_candidate=$PALACE_F1_REPO
elif [[ -n ${SLURM_SUBMIT_DIR:-} && -f $SLURM_SUBMIT_DIR/scripts/f1/common.sh ]]; then
  repo_candidate=$SLURM_SUBMIT_DIR
elif [[ -f "$(dirname -- "${BASH_SOURCE[0]}")/common.sh" ]]; then
  repo_candidate=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
else
  echo "palace-f1: set PALACE_F1_REPO or submit from the Palace checkout root" >&2
  exit 1
fi
repo_candidate=$(cd -- "$repo_candidate" && pwd -P)
export PALACE_F1_REPO=$repo_candidate
# shellcheck source=common.sh
source "$repo_candidate/scripts/f1/common.sh"

repo=$(f1_resolve_repo)
root=${PALACE_F1_ROOT:-"$HOME/opt/palace"}
while (($#)); do
  case "$1" in
    --root)
      (($# >= 2)) || f1_die "--root requires a path"
      root=$2
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      f1_die "unknown argument: $1"
      ;;
  esac
done
case "$root" in
  *$'\n'*|*';'*) f1_die "PALACE_F1_ROOT may not contain newlines or semicolons" ;;
esac
root_requested=$root
root=$(cd -- "$root_requested" && pwd -P) || f1_die "prepared root does not exist: $root_requested"
build_dir=$root/build/$PALACE_F1_BUILD_ID
prefix=$root/versions/$PALACE_F1_BUILD_ID
state_dir=$build_dir/state
lock=$repo/scripts/f1/sources.lock.json
control=$repo/scripts/f1/f1ctl.py
jobs=${SLURM_CPUS_PER_TASK:-}

f1_require_job
[[ $jobs =~ ^[1-9][0-9]*$ ]] || f1_die "SLURM_CPUS_PER_TASK must be a positive integer"
f1_clean_build_environment
f1_load_modules
installer_commit=$(python3 "$control" verify-prepared \
  --repo "$repo" --root "$root" --field installer.git_commit)
source_lock_sha=$(f1_sha256 "$lock")

if [[ -f $prefix/BUILD-RECEIPT.json ]]; then
  python3 "$control" verify-install --prefix "$prefix" --prepared "$root/prepared.json"
  echo "Verified completed installation: $prefix"
  exit 0
fi
[[ ! -e $prefix/BUILD-RECEIPT.md ]] || f1_die "incomplete receipt exists without JSON: $prefix"
mkdir -p "$build_dir" "$state_dir" "$root/logs"

offline_git_config=$build_dir/offline.gitconfig
cat >"$offline_git_config" <<'EOF'
[protocol "file"]
  allow = always
[protocol "http"]
  allow = never
[protocol "https"]
  allow = never
[protocol "ssh"]
  allow = never
EOF
export GIT_CONFIG_GLOBAL=$offline_git_config
export GIT_CONFIG_NOSYSTEM=1
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy

offline_toolchain=$build_dir/f1-offline-toolchain.cmake
cat >"$offline_toolchain" <<'EOF'
# Inherited by CMake child configurations through the CMAKE_TOOLCHAIN_FILE environment variable.
set(FETCHCONTENT_FULLY_DISCONNECTED ON CACHE BOOL "No network access on F1 compute nodes" FORCE)
set(HYPRE_BUILD_UMPIRE OFF CACHE BOOL "CPU build does not use Umpire" FORCE)
set(SUNDIALS_TEST_ENABLE_UNIT_TESTS OFF CACHE BOOL "No dependency tests in install job" FORCE)
set(SUNDIALS_TEST_ENABLE_GTEST OFF CACHE BOOL "No dependency test downloads" FORCE)
set(MFEM_FETCH_TPLS OFF CACHE BOOL "Dependencies are supplied by the Palace superbuild" FORCE)
EOF
export CMAKE_TOOLCHAIN_FILE=$offline_toolchain

plan=$build_dir/BUILD-PLAN.txt
plan_candidate=$build_dir/.BUILD-PLAN.txt.new
cat >"$plan_candidate" <<EOF
build_id=$PALACE_F1_BUILD_ID
source_commit=$PALACE_F1_SOURCE_COMMIT
source_tree=$PALACE_F1_SOURCE_TREE
installer_commit=$installer_commit
source_lock_sha256=$source_lock_sha
gcc_module=$PALACE_F1_GCC_MODULE
mpi_module=$PALACE_F1_MPI_MODULE
cmake=3.31.8
openblas=0.3.20 shared pthread LP64 DYNAMIC_ARCH=1 USE_OPENMP=0 NUM_THREADS=112
zlib=1.3.2 shared
libceed=shared
libxsmm=shared
runtime_gcc=11.2.0
runtime_openmpi=4.1.6
fast_float=00c8c7b0d5c722d2212568d915a39ea73b08b973 installed external for scn
scn=4.0.1 direct offline build
build_type=Release
build_shared_libs=OFF
palace_with_openmp=ON
palace_with_arpack=ON
palace_with_slepc=OFF
palace_with_superlu=ON
palace_with_strumpack=OFF
palace_with_mumps=OFF
palace_with_sundials=ON
palace_with_gslib=ON
palace_with_libxsmm=ON
palace_with_cuda=OFF
palace_with_hip=OFF
palace_with_gpu_aware_mpi=OFF
palace_with_64bit_int=OFF
palace_with_64bit_blas_int=OFF
fetchcontent_fully_disconnected=ON
hypre_build_umpire=OFF
sundials_tests=OFF
mfem_fetch_tpls=OFF
install_prefix=$prefix
EOF
if [[ -f $plan ]]; then
  cmp -s "$plan" "$plan_candidate" || f1_die "existing build directory has a different plan: $build_dir"
  rm -f "$plan_candidate"
else
  mv "$plan_candidate" "$plan"
  chmod 0444 "$plan"
fi

python3 "$control" claim-prefix \
  --prefix "$prefix" \
  --owner-receipt "$state_dir/PREFIX-OWNER.json" \
  --plan "$plan" \
  --installer-commit "$installer_commit" \
  --source-lock-sha256 "$source_lock_sha"

stage_is_complete() {
  local label=$1
  local receipt=$state_dir/$label.receipt.json
  local marker=$state_dir/$label.complete
  if [[ -f $receipt ]]; then
    python3 "$control" verify-stage-receipt \
      --prefix "$prefix" --receipt "$receipt" --label "$label"
    [[ -f $marker ]] || touch "$marker"
    return 0
  fi
  [[ ! -e $marker ]] || f1_die "unverifiable bare stage marker: $marker"
  return 1
}

complete_stage() {
  local label=$1
  python3 "$control" write-stage-receipt \
    --prefix "$prefix" --receipt "$state_dir/$label.receipt.json" --label "$label"
  touch "$state_dir/$label.complete"
}

cmake_bin=$root/cache/tools/cmake-3.31.8-linux-x86_64/bin/cmake
[[ -x $cmake_bin ]] || f1_die "prepared CMake executable missing: $cmake_bin"
[[ $($cmake_bin --version | sed -n '1{s/cmake version //p;q;}') == 3.31.8 ]] || f1_die "CMake bootstrap version mismatch"

source_dir=$(python3 "$control" verify-prepared --repo "$repo" --root "$root" --field palace_source.source_dir)
[[ -d $source_dir/palace && -f $source_dir/CMakeLists.txt ]] || f1_die "prepared Palace source is incomplete"

bootstrap_source=$root/source/bootstrap
mkdir -p "$bootstrap_source"
python3 "$control" extract \
  --archive "$root/cache/archives/OpenBLAS-0.3.20.tar.gz" \
  --destination "$bootstrap_source/openblas-0.3.20" \
  --sha256 8495c9affc536253648e942908e88e097f2ec7753ede55aca52e5dead3029e3c \
  --expected-root OpenBLAS-0.3.20 >/dev/null
python3 "$control" extract \
  --archive "$root/cache/archives/zlib-1.3.2.tar.gz" \
  --destination "$bootstrap_source/zlib-1.3.2" \
  --sha256 bb329a0a2cd0274d05519d61c667c062e06990d72e125ee2dfa8de64f0119d16 \
  --expected-root zlib-1.3.2 >/dev/null
python3 "$control" extract \
  --archive "$root/cache/archives/scn-4.0.1.zip" \
  --destination "$bootstrap_source/scn-4.0.1" \
  --sha256 4e2540e947338ec5f51e89e3b61dc9a7cd0022cece80c15a192253b5ebc573b8 \
  --expected-root scnlib-4.0.1 >/dev/null

if ! stage_is_complete zlib; then
  "$cmake_bin" --fresh \
    -S "$bootstrap_source/zlib-1.3.2/zlib-1.3.2" \
    -B "$build_dir/zlib" \
    -G "Unix Makefiles" \
    -DCMAKE_BUILD_TYPE=Release \
    -DBUILD_SHARED_LIBS=ON \
    -DCMAKE_INSTALL_PREFIX="$prefix" \
    -DCMAKE_INSTALL_RPATH="$prefix/lib;$prefix/lib64"
  "$cmake_bin" --build "$build_dir/zlib" --parallel "$jobs"
  "$cmake_bin" --install "$build_dir/zlib"
  [[ -f $prefix/include/zlib.h ]] || f1_die "zlib header was not installed"
  complete_stage zlib
fi

if ! stage_is_complete openblas; then
  if [[ ! -d $build_dir/OpenBLAS-0.3.20 ]]; then
    cp -a "$bootstrap_source/openblas-0.3.20/OpenBLAS-0.3.20" "$build_dir/OpenBLAS-0.3.20"
    chmod -R u+w "$build_dir/OpenBLAS-0.3.20"
  fi
  make -C "$build_dir/OpenBLAS-0.3.20" --jobs="$jobs" \
    CC=gcc FC=gfortran USE_OPENMP=0 USE_THREAD=1 NUM_THREADS=112 DYNAMIC_ARCH=1
  make -C "$build_dir/OpenBLAS-0.3.20" \
    CC=gcc FC=gfortran USE_OPENMP=0 USE_THREAD=1 NUM_THREADS=112 DYNAMIC_ARCH=1 \
    PREFIX="$prefix" install
  [[ -f $prefix/include/cblas.h ]] || f1_die "OpenBLAS cblas.h was not installed"
  complete_stage openblas
fi

fast_float_source=$build_dir/fast_float-source
if [[ ! -d $fast_float_source/.git ]]; then
  git clone --no-checkout "file://$root/cache/git/fast_float.git" "$fast_float_source"
  git -C "$fast_float_source" checkout --detach 00c8c7b0d5c722d2212568d915a39ea73b08b973
fi
[[ $(git -C "$fast_float_source" rev-parse HEAD) == 00c8c7b0d5c722d2212568d915a39ea73b08b973 ]] || \
  f1_die "fast_float source commit mismatch"
[[ -z $(git -C "$fast_float_source" status --porcelain) ]] || f1_die "fast_float source tree was modified"
if ! stage_is_complete fast_float; then
  "$cmake_bin" --fresh \
    -S "$fast_float_source" \
    -B "$build_dir/fast_float" \
    -G "Unix Makefiles" \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="$prefix" \
    -DFASTFLOAT_TEST=OFF \
    -DFASTFLOAT_INSTALL=ON
  "$cmake_bin" --build "$build_dir/fast_float" --parallel "$jobs"
  "$cmake_bin" --install "$build_dir/fast_float"
  [[ -f $prefix/share/cmake/FastFloat/FastFloatConfig.cmake ]] || \
    f1_die "FastFloat package config was not installed"
  complete_stage fast_float
fi

scn_new_install=0
if ! stage_is_complete scn; then
  "$cmake_bin" --fresh \
    -S "$bootstrap_source/scn-4.0.1/scnlib-4.0.1" \
    -B "$build_dir/scn" \
    -G "Unix Makefiles" \
    -DCMAKE_BUILD_TYPE=Release \
    -DBUILD_SHARED_LIBS=OFF \
    -DCMAKE_INSTALL_PREFIX="$prefix" \
    -DCMAKE_PREFIX_PATH="$prefix" \
    -DSCN_INSTALL=ON \
    -DSCN_REGEX_BACKEND=std \
    -DSCN_DISABLE_TOP_PROJECT=ON \
    -DSCN_USE_EXTERNAL_FAST_FLOAT=ON \
    -DFETCHCONTENT_FULLY_DISCONNECTED=ON
  "$cmake_bin" --build "$build_dir/scn" --parallel "$jobs"
  "$cmake_bin" --install "$build_dir/scn"
  scn_new_install=1
fi
scn_config=$(f1_find_one "$prefix" 'scn-config.cmake')
case "$scn_config" in
  "$prefix"/lib/cmake/scn/scn-config.cmake|"$prefix"/lib64/cmake/scn/scn-config.cmake) ;;
  *) f1_die "scn package config was installed in an unsupported location: $scn_config" ;;
esac
scn_dir=$(dirname -- "$scn_config")
((scn_new_install == 0)) || complete_stage scn

openblas_library=$(f1_find_one "$prefix" 'libopenblas.so')
zlib_library=$(f1_find_one "$prefix" 'libz.so')
export OPENBLAS_ROOT=$prefix
export OPENBLAS_NUM_THREADS=1
export OMP_NUM_THREADS=1
blas_lapack="$openblas_library;-lm"

offline_include=$build_dir/offline-external-project.cmake
cat >"$offline_include" <<'EOF'
set_property(DIRECTORY PROPERTY EP_UPDATE_DISCONNECTED ON)
EOF

local_url() {
  printf 'file://%s' "$1"
}

superbuild=$build_dir/superbuild
"$cmake_bin" --fresh -S "$source_dir" -B "$superbuild" -G "Unix Makefiles" \
  -DCMAKE_BUILD_TYPE=Release \
  -DBUILD_SHARED_LIBS=OFF \
  -DCMAKE_INSTALL_PREFIX="$prefix" \
  -DCMAKE_INSTALL_RPATH="$prefix/lib;$prefix/lib64" \
  -DCMAKE_INSTALL_RPATH_USE_LINK_PATH=ON \
  -DCMAKE_PREFIX_PATH="$prefix" \
  -DCMAKE_C_COMPILER="$(command -v mpicc)" \
  -DCMAKE_CXX_COMPILER="$(command -v mpicxx)" \
  -DCMAKE_Fortran_COMPILER="$(command -v mpifort)" \
  -DCMAKE_PROJECT_INCLUDE="$offline_include" \
  -DZLIB_ROOT="$prefix" \
  -DZLIB_INCLUDE_DIR="$prefix/include" \
  -DZLIB_LIBRARY="$zlib_library" \
  -DBLAS_LIBRARIES="$blas_lapack" \
  -DLAPACK_LIBRARIES="$blas_lapack" \
  -DPALACE_BUILD_EXTERNAL_DEPS=ON \
  -DPALACE_WITH_OPENMP=ON \
  -DPALACE_WITH_ARPACK=ON \
  -DPALACE_WITH_SLEPC=OFF \
  -DPALACE_WITH_SUPERLU=ON \
  -DPALACE_WITH_STRUMPACK=OFF \
  -DPALACE_WITH_MUMPS=OFF \
  -DPALACE_WITH_SUNDIALS=ON \
  -DPALACE_WITH_GSLIB=ON \
  -DPALACE_WITH_LIBXSMM=ON \
  -DPALACE_WITH_MAGMA=OFF \
  -DPALACE_WITH_CUDA=OFF \
  -DPALACE_WITH_HIP=OFF \
  -DPALACE_WITH_GPU_AWARE_MPI=OFF \
  -DPALACE_WITH_64BIT_INT=OFF \
  -DPALACE_WITH_64BIT_BLAS_INT=OFF \
  -DEXTERN_ARPACK_URL="$(local_url "$root/cache/git/arpack-ng.git")" \
  -DEXTERN_ARPACK_GIT_TAG=804fa3149a0f773064198a8e883bd021832157ca \
  -DEXTERN_GSLIB_URL="$(local_url "$root/cache/git/gslib.git")" \
  -DEXTERN_GSLIB_GIT_TAG=95acf5b42301d6cb48fda88d662f1d784b863089 \
  -DEXTERN_HYPRE_URL="$(local_url "$root/cache/git/hypre.git")" \
  -DEXTERN_HYPRE_GIT_TAG=fca98afcd78a26b784dc24b0c3edb0e6140598af \
  -DEXTERN_LIBCEED_URL="$(local_url "$root/cache/git/libCEED.git")" \
  -DEXTERN_LIBCEED_GIT_TAG=95bd1e908b16e04a70015e3a9a7fddec5e9c3fc8 \
  -DEXTERN_LIBXSMM_URL="$(local_url "$root/cache/git/libxsmm.git")" \
  -DEXTERN_LIBXSMM_GIT_TAG=ea0b20499a41377bab148257240adbbfe1b4a333 \
  -DEXTERN_METIS_URL="$(local_url "$root/cache/git/metis.git")" \
  -DEXTERN_METIS_GIT_TAG=08c3082720ff9114b8e3cbaa4484a26739cd7d2d \
  -DEXTERN_MFEM_URL="$(local_url "$root/cache/git/mfem.git")" \
  -DEXTERN_MFEM_GIT_TAG=d9d6526cc1749980a2ba1da16e2c1ca1e07d82ec \
  -DEXTERN_PARMETIS_URL="$(local_url "$root/cache/git/parmetis.git")" \
  -DEXTERN_PARMETIS_GIT_TAG=53c9341b6c1ba876c97567cb52ddfc87c159dc36 \
  -DEXTERN_SUNDIALS_URL="$(local_url "$root/cache/git/sundials.git")" \
  -DEXTERN_SUNDIALS_GIT_TAG=c8dabcea90ca8bf195474da120e4f3dd39aa711f \
  -DEXTERN_SUPERLU_URL="$(local_url "$root/cache/git/superlu_dist.git")" \
  -DEXTERN_SUPERLU_GIT_TAG=39236c165c6887da09b05c0f540478378ad34d38 \
  -DEXTERN_JSON_URL="$(local_url "$root/cache/archives/json-3.12.0.tar.xz")" \
  -DEXTERN_JSON_SCHEMA_VALIDATOR_URL="$(local_url "$root/cache/archives/json-schema-validator-2.4.0.tar.gz")" \
  -DEXTERN_FMT_URL="$(local_url "$root/cache/archives/fmt-12.1.0.zip")" \
  -DEXTERN_SCN_URL="$(local_url "$root/cache/archives/scn-4.0.1.zip")" \
  -DEXTERN_EIGEN_URL="$(local_url "$root/cache/archives/eigen-5.0.0.tar.gz")"

grep -E '^BLAS_LAPACK_LIBRARIES:STRING=' "$superbuild/CMakeCache.txt" | grep -Fq "$prefix" || \
  f1_die "superbuild did not bind BLAS/LAPACK to the versioned OpenBLAS prefix"
grep -E '^ZLIB_LIBRARY[^=]*=' "$superbuild/CMakeCache.txt" | grep -Fq "$prefix" || \
  f1_die "superbuild did not bind zlib to the versioned prefix"

"$cmake_bin" --build "$superbuild" --parallel "$jobs" --target \
  mfem libCEED arpack-ng json_schema_validator fmt eigen

native=$build_dir/native
"$cmake_bin" --fresh -S "$source_dir/palace" -B "$native" -G "Unix Makefiles" \
  -DCMAKE_BUILD_TYPE=Release \
  -DBUILD_SHARED_LIBS=OFF \
  -DCMAKE_INSTALL_PREFIX="$prefix" \
  -DCMAKE_PREFIX_PATH="$prefix" \
  -DCMAKE_INSTALL_RPATH="$prefix/lib;$prefix/lib64" \
  -DCMAKE_INSTALL_RPATH_USE_LINK_PATH=ON \
  -DCMAKE_C_COMPILER="$(command -v mpicc)" \
  -DCMAKE_CXX_COMPILER="$(command -v mpicxx)" \
  -DCMAKE_Fortran_COMPILER="$(command -v mpifort)" \
  -DPALACE_BUILD_EXTERNAL_DEPS=OFF \
  -DFETCHCONTENT_FULLY_DISCONNECTED=ON \
  -DCMAKE_DISABLE_FIND_PACKAGE_Catch2=TRUE \
  -DPALACE_WITH_OPENMP=ON \
  -DPALACE_WITH_ARPACK=ON \
  -DPALACE_WITH_SLEPC=OFF \
  -DPALACE_WITH_SUPERLU=ON \
  -DPALACE_WITH_STRUMPACK=OFF \
  -DPALACE_WITH_MUMPS=OFF \
  -DPALACE_WITH_SUNDIALS=ON \
  -DPALACE_WITH_GSLIB=ON \
  -DPALACE_WITH_CUDA=OFF \
  -DPALACE_WITH_HIP=OFF \
  -DPALACE_WITH_GPU_AWARE_MPI=OFF \
  -DPALACE_WITH_64BIT_INT=OFF \
  -DPALACE_WITH_64BIT_BLAS_INT=OFF \
  -DMFEM_DIR="$prefix" \
  -DLIBCEED_DIR="$prefix" \
  -DMETIS_DIR="$prefix" \
  -DPARMETIS_DIR="$prefix" \
  -DHYPRE_DIR="$prefix" \
  -DSUPERLU_DIST_DIR="$prefix" \
  -DSUNDIALS_DIR="$prefix" \
  -DGSLIB_DIR="$prefix" \
  -DARPACK_DIR="$prefix" \
  -Dscn_DIR="$scn_dir" \
  -DBLAS_LIBRARIES="$blas_lapack" \
  -DLAPACK_LIBRARIES="$blas_lapack" \
  -DZLIB_INCLUDE_DIR="$prefix/include" \
  -DZLIB_LIBRARY="$zlib_library"
grep -E '^BLAS_LIBRARIES:[^=]*=' "$native/CMakeCache.txt" | grep -Fq "$prefix" || \
  f1_die "native build did not bind BLAS to the versioned OpenBLAS prefix"
grep -E '^LAPACK_LIBRARIES:[^=]*=' "$native/CMakeCache.txt" | grep -Fq "$prefix" || \
  f1_die "native build did not bind LAPACK to the versioned OpenBLAS prefix"
"$cmake_bin" --build "$native" --parallel "$jobs" --target palace
"$cmake_bin" --install "$native" --strip

binary=$(f1_find_one "$prefix/bin" 'palace-*.bin')
[[ -d $prefix/include/palace ]] || f1_die "installed libCEED JIT header directory is missing"
find "$prefix/include/palace" -type f -print -quit | grep -q . || f1_die "installed libCEED JIT headers are missing"

run_env=$prefix/run-env.sh
{
  echo '# Source after loading the F1 GCC and Open MPI modules.'
  printf 'export PALACE_F1_PREFIX=%q\n' "$prefix"
  printf 'export PALACE_F1_EXECUTABLE=%q\n' "$binary"
  printf 'export LD_LIBRARY_PATH=%q/lib:%q/lib64${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}\n' "$prefix" "$prefix"
  echo 'export OPENBLAS_NUM_THREADS=1'
  echo 'export OMP_NUM_THREADS=${OMP_NUM_THREADS:-1}'
} >"$run_env"
chmod 0555 "$run_env"

ldd_file=$prefix/BUILD-ELF-LDD.txt
readelf_file=$prefix/BUILD-ELF-READELF.txt
ldd "$binary" >"$ldd_file"
! grep -q 'not found' "$ldd_file" || f1_die "installed executable has unresolved runtime libraries"
readelf -d "$binary" >"$readelf_file"
chmod 0444 "$ldd_file" "$readelf_file"
cp "$offline_toolchain" "$prefix/BUILD-OFFLINE-TOOLCHAIN.cmake"
chmod 0444 "$prefix/BUILD-OFFLINE-TOOLCHAIN.cmake"

python3 "$control" write-build-receipt \
  --prefix "$prefix" \
  --build-dir "$build_dir" \
  --root "$root" \
  --build-id "$PALACE_F1_BUILD_ID" \
  --plan "$plan" \
  --cmake "$cmake_bin" \
  --ldd "$ldd_file" \
  --readelf "$readelf_file" >/dev/null
chmod -R a-w "$prefix"
python3 "$control" verify-install --prefix "$prefix" --prepared "$root/prepared.json"
touch "$state_dir/complete"
echo "Completed immutable Palace installation: $prefix"
echo "Direct executable: $binary"
