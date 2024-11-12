function no
    nvim $(fd $argv | fzf --preview "cat {}")
end
