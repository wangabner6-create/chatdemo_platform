# Continuous delivery

The repository uses three GitHub Actions workflows to keep development,
evaluation, and release tied to the same commit.

```text
pull request
  -> CI / Code quality and tests
  -> CI / Deployment image (build + /health smoke test)
  -> Observability evaluation / Evaluation quality gate
  -> protected main allows merge only when all three pass

merge to main
  -> CI and Observability evaluation run again for the merge commit
  -> Deploy verified image confirms both successful run IDs for that exact SHA
  -> build + local /health smoke test
  -> push immutable GHCR tag
  -> pull the published digest + second /health smoke test
  -> promote the verified digest to latest
```

## Merge gate

`main` is protected with strict status checks. The required check names are:

- `Code quality and tests`
- `Deployment image`
- `Evaluation quality gate`

The evaluation command exits non-zero when a required quality threshold or
allowed baseline regression is breached. Because that job is required, a red
evaluation keeps the pull request's merge button disabled.

## Release gate

`.github/workflows/deploy.yml` is triggered only by a successful
`Observability evaluation` run whose original event was a push to `main`.
Before publishing it:

1. compares the evaluated SHA with the current `main` SHA, skipping stale runs;
2. queries GitHub Actions for successful `CI` and `Observability evaluation`
   push runs on that exact SHA;
3. builds and starts the candidate image;
4. publishes `ghcr.io/wangabner6-create/chatdemo_platform:sha-<full-sha>`;
5. pulls the registry digest back and starts it again; and
6. checks the current `main` SHA again, then updates
   `ghcr.io/wangabner6-create/chatdemo_platform:latest` only when the published
   digest passed `/health` and is still the newest commit.

The workflow uses the repository-scoped `GITHUB_TOKEN`; no long-lived registry
password is required. GitHub records the run, source commit, upstream gate run
links, image digest, and package link in the workflow summary.

## Runtime hosting boundary

The automated target in this repository is GHCR, which is the verified release
artifact boundary. A continuously running public service still needs a runtime
target such as Render or a managed server. Connect that target only after its
account, region, environment variables, and secret-management policy are
chosen; it should deploy the immutable digest produced by this workflow rather
than rebuilding an unverified branch.
