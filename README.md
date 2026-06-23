<a name="readme-top"></a>

# 

--------------------------------------------------------------------------------------------------------------------------

## Automated architecture diagram

This template now includes an automated architecture diagram process:

- `scripts/generate_architecture_diagram.py` scans source files and docs and writes `docs/architecture.mmd`.
- `.github/workflows/update-architecture-diagram.yml` regenerates and commits `docs/architecture.mmd` on every push.
- `.github/workflows/check-architecture-diagram.yml` ensures pull requests have an up-to-date architecture diagram.

### Local usage

```bash
python scripts/generate_architecture_diagram.py
python scripts/generate_architecture_diagram.py --check
```

--------------------------------------------------------------------------------------------------------------------------
== We're Using GitHub Under Protest ==

This project is currently hosted on GitHub.  This is not ideal; GitHub is a
proprietary, trade-secret system that is not Free and Open Souce Software
(FOSS).  We are deeply concerned about using a proprietary system like GitHub
to develop our FOSS project. I have a [website](https://bellKevin.me) where the
project contributors are actively discussing how we can move away from GitHub
in the long term.  We urge you to read about the [Give up GitHub](https://GiveUpGitHub.org) campaign 
from [the Software Freedom Conservancy](https://sfconservancy.org) to understand some of the reasons why GitHub is not 
a good place to host FOSS projects.

If you are a contributor who personally has already quit using GitHub, please
email me at **kevinBell@Linux.com** for how to send us contributions without
using GitHub directly.

Any use of this project's code by GitHub Copilot, past or present, is done
without our permission.  We do not consent to GitHub's use of this project's
code in Copilot.

![Logo of the GiveUpGitHub campaign](https://sfconservancy.org/img/GiveUpGitHub.png)

<p align="right"><a href="#readme-top">back to top</a></p>
