# Rebuilding a running image's tag lost the image (Docker Desktop, containerd image store)

**Problem.** The first version of the deploy gate tagged the running production image for a rollback *after* building
the new one. On the second throwaway deploy of the same commit, `docker tag <running image id> ...` failed with
`No such image`, although the container still ran on it.

**Cause.** The build reused the tag (`settleview:<sha12>`) of the running image. With Docker Desktop's containerd image
store, the old image lost its only name, and an unnamed image is not kept just because a container uses it.

**Fix.** The gate builds `settleview:<sha12>` and moves `settleview:local` (what production's compose file runs by
default) only after production passed the smoke suite, so production's own image keeps its name until then. A rollback
is redeploying the previous release (its commit, rebuilt, or its image by digest from the registry), never a local
image that may be gone.

**How to avoid.** Never rely on an untagged image surviving; keep a name on anything you may need again, or take it
from a registry by digest.
