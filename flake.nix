{
  description = "Swann NVR Home Assistant integration - dev shell";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { nixpkgs, ... }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];
      forAll = f: nixpkgs.lib.genAttrs systems (s: f nixpkgs.legacyPackages.${s});
    in
    {
      devShells = forAll (pkgs: {
        default = pkgs.mkShell {
          # Home Assistant 2026.10 requires Python >= 3.14.2.
          packages = [
            pkgs.python314
            pkgs.uv
            pkgs.ruff
          ];
          env = {
            UV_PYTHON_DOWNLOADS = "never";
            # manylinux wheels (numpy, av, ...) need these on NixOS.
            LD_LIBRARY_PATH = pkgs.lib.makeLibraryPath [
              pkgs.stdenv.cc.cc.lib
              pkgs.zlib
            ];
          };
          shellHook = ''
            if [ ! -d .venv ]; then
              uv venv --python ${pkgs.python314}/bin/python3.14 .venv
              uv pip install -r requirements_test.txt
            fi
            source .venv/bin/activate
            # HA pulls a generic-linux uv wheel into the venv; keep the nix one first.
            export PATH=${pkgs.uv}/bin:$PATH
          '';
        };
      });
    };
}
