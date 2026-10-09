{
  description = "OpenCoord: RF Explorer spectrum scanner and wireless mic / IEM frequency coordinator";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.uv2nix.follows = "uv2nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      pyproject-nix,
      uv2nix,
      pyproject-build-systems,
      ...
    }:
    let
      inherit (nixpkgs) lib;

      systems = [
        "x86_64-linux"
        "aarch64-linux"
        # x86_64-darwin is gone: nixpkgs 26.11 (nixos-unstable) dropped it.
        "aarch64-darwin"
      ];
      forAllSystems = f: lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});

      # Same uv.lock as `uv sync`, so Nix and uv resolve identical versions.
      workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = ./.; };
      overlay = workspace.mkPyprojectOverlay { sourcePreference = "wheel"; };

      # Libraries Dear PyGui (GLFW) dlopens at runtime on Linux.
      runtimeLibs =
        pkgs:
        lib.optionals pkgs.stdenv.hostPlatform.isLinux (
          with pkgs;
          [
            libGL
            libx11
            libxrandr
            libxinerama
            libxcursor
            libxi
            libxext
            libxkbcommon
            wayland
          ]
        );

      # Fixups for binary wheels from PyPI.
      pyprojectOverrides =
        pkgs: final: prev:
        lib.optionalAttrs pkgs.stdenv.hostPlatform.isLinux {
          # The manylinux wheel links libX11 and libstdc++ directly.
          dearpygui = prev.dearpygui.overrideAttrs (old: {
            nativeBuildInputs = (old.nativeBuildInputs or [ ]) ++ [ pkgs.autoPatchelfHook ];
            buildInputs = (old.buildInputs or [ ]) ++ [
              pkgs.libx11
              pkgs.stdenv.cc.cc.lib
            ];
          });
        };

      pythonSets = forAllSystems (
        pkgs:
        (pkgs.callPackage pyproject-nix.build.packages { python = pkgs.python313; }).overrideScope (
          lib.composeManyExtensions [
            pyproject-build-systems.overlays.wheel
            overlay
            (pyprojectOverrides pkgs)
          ]
        )
      );

      mkVenv =
        pkgs: name: deps:
        pythonSets.${pkgs.stdenv.hostPlatform.system}.mkVirtualEnv name deps;
    in
    {
      packages = forAllSystems (
        pkgs:
        let
          venv = mkVenv pkgs "opencoord-env" workspace.deps.default;
          pname = "opencoord";
          version = pythonSets.${pkgs.stdenv.hostPlatform.system}.opencoord.version;
          # GL/X libs are prepended. Appended last: the NixOS driver dir, then nixpkgs mesa as a
          # fallback so the app also runs on non-NixOS hosts, where libglvnd otherwise finds no GLX
          # vendor library. Anything already on LD_LIBRARY_PATH (e.g. nixGL) still wins.
          linuxWrapperArgs = lib.optionalString pkgs.stdenv.hostPlatform.isLinux (
            lib.escapeShellArgs [
              "--prefix"
              "LD_LIBRARY_PATH"
              ":"
              (lib.makeLibraryPath (runtimeLibs pkgs))
              "--suffix"
              "LD_LIBRARY_PATH"
              ":"
              "/run/opengl-driver/lib:${lib.makeLibraryPath [ pkgs.mesa ]}"
            ]
          );
        in
        rec {
          opencoord = pkgs.stdenvNoCC.mkDerivation {
            inherit pname version;
            dontUnpack = true;
            nativeBuildInputs = [ pkgs.makeWrapper ];
            installPhase = ''
              runHook preInstall
              for bin in opencoord opencoord-cli; do
                makeWrapper ${venv}/bin/$bin $out/bin/$bin ${linuxWrapperArgs}
              done
              install -Dm644 ${./packaging/linux/opencoord.desktop} $out/share/applications/opencoord.desktop
              install -Dm644 ${./packaging/linux/99-opencoord-rfexplorer.rules} \
                $out/lib/udev/rules.d/99-opencoord-rfexplorer.rules
              runHook postInstall
            '';
            meta = {
              description = "RF Explorer spectrum scanner and wireless mic / IEM frequency coordinator";
              homepage = "https://github.com/Waayway/opencoord";
              license = lib.licenses.gpl3Plus;
              mainProgram = "opencoord";
              platforms = systems;
            };
          };
          default = opencoord;
        }
      );

      apps = forAllSystems (pkgs: {
        default = {
          type = "app";
          program = lib.getExe self.packages.${pkgs.stdenv.hostPlatform.system}.default;
          meta.description = "Launch the OpenCoord GUI";
        };
      });

      checks = forAllSystems (
        pkgs:
        let
          # Only the test tools from the dev group (not pyinstaller/mypy/ruff).
          testVenv = mkVenv pkgs "opencoord-test-env" (
            workspace.deps.default
            // {
              pytest = [ ];
              hypothesis = [ ];
            }
          );
        in
        {
          pytest = pkgs.stdenvNoCC.mkDerivation {
            name = "opencoord-pytest";
            src = lib.fileset.toSource {
              root = ./.;
              fileset = lib.fileset.unions [
                ./pyproject.toml
                ./tests
                ./profiles
              ];
            };
            nativeBuildInputs = [ testVenv ];
            dontConfigure = true;
            dontBuild = true;
            doCheck = true;
            checkPhase = ''
              runHook preCheck
              export HOME=$(mktemp -d)
              pytest -m "not ui and not hardware" -p no:cacheprovider
              runHook postCheck
            '';
            installPhase = "touch $out";
          };
        }
      );

      devShells = forAllSystems (
        pkgs:
        let
          python = pkgs.python313;
        in
        {
          default = pkgs.mkShell {
            packages = [
              pkgs.uv
              python
              pkgs.ruff
              pkgs.nixfmt
            ];
            env = {
              UV_PYTHON_DOWNLOADS = "never";
              UV_PYTHON = python.interpreter;
            }
            // lib.optionalAttrs pkgs.stdenv.hostPlatform.isLinux {
              # uv's unpatched dearpygui wheel also needs libstdc++; GL fallback order as in the package.
              LD_LIBRARY_PATH = lib.concatStringsSep ":" [
                (lib.makeLibraryPath (runtimeLibs pkgs ++ [ pkgs.stdenv.cc.cc.lib ]))
                "/run/opengl-driver/lib"
                (lib.makeLibraryPath [ pkgs.mesa ])
              ];
            };
            shellHook = ''
              unset PYTHONPATH
            '';
          };
        }
      );

      # NixOS: `programs.opencoord.enable = true;` installs the app and its udev rule. The rule
      # uses TAG+="uaccess", so the logged-in user gets access to the RF Explorer without joining
      # a group (dialout/uucp is not needed).
      nixosModules.default =
        {
          config,
          lib,
          pkgs,
          ...
        }:
        let
          cfg = config.programs.opencoord;
        in
        {
          options.programs.opencoord = {
            enable = lib.mkEnableOption "OpenCoord, the RF Explorer spectrum scanner and frequency coordinator";
            package = lib.mkOption {
              type = lib.types.package;
              default = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
              defaultText = lib.literalExpression "opencoord.packages.\${pkgs.stdenv.hostPlatform.system}.default";
              description = "The OpenCoord package to install.";
            };
          };

          config = lib.mkIf cfg.enable {
            environment.systemPackages = [ cfg.package ];
            services.udev.packages = [ cfg.package ];
          };
        };

      formatter = forAllSystems (pkgs: pkgs.nixfmt);
    };
}
