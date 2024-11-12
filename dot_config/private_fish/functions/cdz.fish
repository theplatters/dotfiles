function cdz
    cd $(dirname $(fd $argv | fzf --preview "cat {}"))
end
