# `docker compose build`: "additional privileges requested: pass --allow=fs.read=Dockerfile"

**Problem.** On Docker Desktop (Compose v5, which always builds through Buildx Bake) `docker compose build` and
`docker compose up --build` stopped at once with `additional privileges requested: pass "--allow=fs.read=Dockerfile"
to grant requested privileges`. `COMPOSE_BAKE=false` no longer helps (v5 has no classic builder path).

**Cause.** Not the compose file. The shell's folder was typed in a different letter case from the real one
(`C:\Users\<you>\documents\<checkout>`; the folder is `Documents`). `docker compose build --print` shows the Bake
target with `"context": "...\documents\<checkout>"` but `"dockerfile": "...\Documents\<checkout>\Dockerfile"`:
compose resolves the Dockerfile to its real path and keeps the context as typed. Bake compares paths case-sensitively,
decides the Dockerfile lies outside the context, and asks for a filesystem-read entitlement. Writing `build: {context:
., dockerfile: Dockerfile}` changes nothing (both are made absolute the same way). From the correctly cased folder
the same file builds.

**Fix.** `cd` to the folder with its real case (in PowerShell, `cd $HOME\Documents\<checkout>`; a tab completion
gives the real case), or build without compose: `docker build -t settleview:local .`, then `docker compose up -d`
(without `--build`; compose uses the existing `settleview:local` image). compose.yaml's header and
docs/operations.md ("Docker") say so.

**How to avoid.** When a Bake entitlement error names a file inside the project, run `docker compose build --print`
and compare the `context` and `dockerfile` paths character by character before touching the compose file.
