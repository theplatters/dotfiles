set -gx FZF_DEFAULT_COMMAND 'fd . --hidden'
set -gx EDITOR nvim
oh-my-posh init fish --config https://github.com/JanDeDobbeleer/oh-my-posh/blob/main/themes/catppuccin_macchiato.omp.json | source
eval (opam env)
alias gpom="git push origin main"
