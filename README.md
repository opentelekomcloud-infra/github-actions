# github-actions Workflow Templates

This repository contains central workflows to be used in our Ecosystem infrastructure.

## Update Major Tag version to current tag

It is necessary to update latest Major version to current tag, to keep Github actions working with the latest update.

```bash
git tag -f v1 v1.0.2
git push origin -f v1
