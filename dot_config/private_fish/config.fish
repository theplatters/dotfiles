set -gx FZF_DEFAULT_COMMAND 'fd . --hidden'
set -gx EDITOR nvim
oh-my-posh init fish --config "https://github.com/JanDeDobbeleer/oh-my-posh/blob/main/themes/gruvbox.omp.json" | source
fish_add_path ~/.julia/bin
eval (opam env)
alias gpom="git push origin main"

# Added by Antigravity CLI installer
set -gx PATH "/home/franzs/.local/bin" $PATH

fish_add_path /home/franzs/.local/bin
