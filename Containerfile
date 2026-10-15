# The docker backend's image: the kexplore tools, with no host-specific files.
#
# The container explores the *host* kernel. run.sh starts it with
# --cap-add SYS_RAWIO plus an unmask flag (see detect.sh) so drgn can read
# /proc/kcore, with the repo mounted read-only at the same path, and with
# the debuginfod cache as a volume so the kernel DWARF (hundreds of MB,
# downloaded without resume) is kept between runs.
#
#   docker build -t kexplore -f Containerfile .
#
# Fedora as the base because its packages are what the lima VM provisions,
# so every backend runs the same tool versions.
FROM fedora:44

RUN dnf install -y \
      drgn \
      elfutils-debuginfod-client \
      dwarves \
      binutils \
      python3-textual \
    && dnf clean all

# bpftrace is not installed: tracing from a container needs --privileged and
# tracefs mounts, so measurements are available only on the lima and native
# backends, and kexplore treats them as optional.
