# Template updates

sonde started from the [scaffold](https://github.com/Jartan-LLC/scaffold) template.
`.scaffold-sync` holds the newest scaffold commit that has been ported here or
deliberately skipped.

```bash
git remote add upstream https://github.com/Jartan-LLC/scaffold.git  # once
git fetch upstream
git log --oneline --reverse "$(cat .scaffold-sync)"..upstream/main  # not yet dealt with
git show <sha>                 # port the change by hand
echo <sha> > .scaffold-sync    # commit it with the port
```
