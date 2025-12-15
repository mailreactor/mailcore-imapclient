{
  description = "mailcore-imapclient - IMAPClient adapter for mailcore";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
      in
      {
        devShells.default = pkgs.mkShell {
          buildInputs = with pkgs; [
            python310
            uv
          ];

          shellHook = ''
            echo
            echo "🌟 mailcore-imapclient Development Environment"
            echo "$(python --version) | $(uv --version)"
            
            # Only show quick start if no virtual env exists (first time)
            if [ ! -d .venv ]; then
              echo
              echo "Quick start:"
              echo "  uv venv --python $(which python)"
              echo "  source .venv/bin/activate" 
              echo "  uv pip install -e ../mailcore  # Install mailcore in editable mode"
              echo "  uv pip install -e \".[dev]\""
              echo "  python verify-setup.sh"
              echo
            else
              echo "Enable venv with: source .venv/bin/activate" 
              echo "Manual pre-commit check with: pre-commit run --all-files --show-diff-on-failure"
              echo 
            fi
          '';
        };
      }
    );
}
