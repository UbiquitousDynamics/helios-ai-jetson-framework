sudo apt install build-essential pkg-config libssl-dev
CARGO_BUILD_TARGET=aarch64-unknown-linux-gnu pip install --no-binary :all: flagembedding


  Using cached flagembedding-1.3.4-py3-none-any.whl
INFO: pip is looking at multiple versions of flagembedding to determine which version is compatible with other requirements. This could take a while.
  Downloading FlagEmbedding-1.3.3.tar.gz (161 kB)
  Preparing metadata (setup.py) ... done
  Downloading FlagEmbedding-1.3.2.tar.gz (177 kB)
  Preparing metadata (setup.py) ... done
  Downloading FlagEmbedding-1.2.11.tar.gz (147 kB)
  Preparing metadata (setup.py) ... done
  Downloading FlagEmbedding-1.2.10.tar.gz (141 kB)
  Preparing metadata (setup.py) ... done
  Downloading FlagEmbedding-1.2.9.tar.gz (140 kB)
  Preparing metadata (setup.py) ... done
  Downloading FlagEmbedding-1.2.8.tar.gz (120 kB)
  Preparing metadata (setup.py) ... done
  Downloading FlagEmbedding-1.2.7.tar.gz (120 kB)
  Preparing metadata (setup.py) ... done
INFO: pip is still looking at multiple versions of flagembedding to determine which version is compatible with other requirements. This could take a while.
  Downloading FlagEmbedding-1.2.5.tar.gz (37 kB)
  Preparing metadata (setup.py) ... done
  Downloading FlagEmbedding-1.2.3.tar.gz (37 kB)
  Preparing metadata (setup.py) ... done
  Downloading FlagEmbedding-1.2.2.tar.gz (37 kB)
  Preparing metadata (setup.py) ... done
  Downloading FlagEmbedding-1.2.1.tar.gz (37 kB)
  Preparing metadata (setup.py) ... done
  Downloading FlagEmbedding-1.2.0.tar.gz (37 kB)
  Preparing metadata (setup.py) ... done
INFO: This is taking longer than usual. You might need to provide the dependency resolver with stricter constraints to reduce runtime. See https://pip.pypa.io/warnings/backtracking for guidance. If you want to abort this run, press Ctrl + C.
  Downloading FlagEmbedding-1.1.9.tar.gz (27 kB)
  Preparing metadata (setup.py) ... done
  Downloading FlagEmbedding-1.1.8.tar.gz (26 kB)
  Preparing metadata (setup.py) ... done
  Downloading FlagEmbedding-1.1.7.tar.gz (26 kB)
  Preparing metadata (setup.py) ... done
  Downloading FlagEmbedding-1.1.6.tar.gz (26 kB)
  Preparing metadata (setup.py) ... done
  Downloading FlagEmbedding-1.1.5.tar.gz (38 kB)
  Preparing metadata (setup.py) ... done
  Downloading FlagEmbedding-1.1.4.tar.gz (38 kB)
  Preparing metadata (setup.py) ... done
  Downloading FlagEmbedding-1.1.3.tar.gz (36 kB)
  Preparing metadata (setup.py) ... done
  Downloading FlagEmbedding-1.1.2.tar.gz (35 kB)
  Preparing metadata (setup.py) ... done
  Downloading FlagEmbedding-1.1.1.tar.gz (34 kB)
  Preparing metadata (setup.py) ... done
  Downloading FlagEmbedding-1.1.0.tar.gz (34 kB)
  Preparing metadata (setup.py) ... done
  Downloading FlagEmbedding-1.0.7.tar.gz (28 kB)
  Preparing metadata (setup.py) ... done
  Downloading FlagEmbedding-1.0.6.tar.gz (28 kB)
  Preparing metadata (setup.py) ... done
  Downloading FlagEmbedding-1.0.4.tar.gz (27 kB)
  Preparing metadata (setup.py) ... done
  Downloading FlagEmbedding-1.0.3.tar.gz (20 kB)
  Preparing metadata (setup.py) ... done
  Downloading FlagEmbedding-1.0.2.tar.gz (21 kB)
  Preparing metadata (setup.py) ... done
  Downloading FlagEmbedding-1.0.1.tar.gz (21 kB)
  Preparing metadata (setup.py) ... done
ERROR: Cannot install flagembedding==1.0.1, flagembedding==1.0.2, flagembedding==1.0.3, flagembedding==1.0.4, flagembedding==1.0.6, flagembedding==1.0.7, flagembedding==1.1.0, flagembedding==1.1.1, flagembedding==1.1.2, flagembedding==1.1.3, flagembedding==1.1.4, flagembedding==1.1.5, flagembedding==1.1.6, flagembedding==1.1.7, flagembedding==1.1.8, flagembedding==1.1.9, flagembedding==1.2.0, flagembedding==1.2.1, flagembedding==1.2.10, flagembedding==1.2.11, flagembedding==1.2.2, flagembedding==1.2.3, flagembedding==1.2.5, flagembedding==1.2.7, flagembedding==1.2.8, flagembedding==1.2.9, flagembedding==1.3.2, flagembedding==1.3.3 and flagembedding==1.3.4 because these package versions have conflicting dependencies.

The conflict is caused by:
    flagembedding 1.3.4 depends on torch>=1.6.0
    flagembedding 1.3.3 depends on torch>=1.6.0
    flagembedding 1.3.2 depends on torch>=1.6.0
    flagembedding 1.2.11 depends on torch>=1.6.0
    flagembedding 1.2.10 depends on torch>=1.6.0
    flagembedding 1.2.9 depends on torch>=1.6.0
    flagembedding 1.2.8 depends on torch>=1.6.0
    flagembedding 1.2.7 depends on torch>=1.6.0
    flagembedding 1.2.5 depends on torch>=1.6.0
    flagembedding 1.2.3 depends on torch>=1.6.0
    flagembedding 1.2.2 depends on torch>=1.6.0
    flagembedding 1.2.1 depends on torch>=1.6.0
    flagembedding 1.2.0 depends on torch>=1.6.0
    flagembedding 1.1.9 depends on torch>=1.6.0
    flagembedding 1.1.8 depends on torch>=1.6.0
    flagembedding 1.1.7 depends on torch>=1.6.0
    flagembedding 1.1.6 depends on torch>=1.6.0
    flagembedding 1.1.5 depends on torch>=1.6.0
    flagembedding 1.1.4 depends on torch>=1.6.0
    flagembedding 1.1.3 depends on torch>=1.6.0
    flagembedding 1.1.2 depends on torch>=1.6.0
    flagembedding 1.1.1 depends on torch>=1.6.0
    flagembedding 1.1.0 depends on torch>=1.6.0
    flagembedding 1.0.7 depends on torch>=1.6.0
    flagembedding 1.0.6 depends on torch>=1.6.0
    flagembedding 1.0.4 depends on torch>=1.6.0
    flagembedding 1.0.3 depends on torch>=1.6.0
    flagembedding 1.0.2 depends on torch>=1.6.0
    flagembedding 1.0.1 depends on torch>=1.6.0

To fix this you could try to:
1. loosen the range of package versions you've specified
2. remove package versions to allow pip to attempt to solve the dependency conflict

ERROR: ResolutionImpossible: for help visit https://pip.pypa.io/en/latest/topics/dependency-resolution/#dealing-with-dependency-conflicts