function cdz
    set file (fd $argv | fzf --preview "cat {}")
    if test -d $file
        cd $file
    else
        cd $(dirname $file)
    end
end
