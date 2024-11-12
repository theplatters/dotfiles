function cdf
    set -l search_dir $argv[1]
    set -l target_name $argv[2]
    # Find the first match

    if test "$argv[3]" = --hidden
        set hidden_flag --hidden
    end

    set -l result (fd -g $target_name $search_dir $hidden_flag | grep -v ".cache" | head -1)

    # Check if a result was found
    if test -z "$result"
        echo "No file or directory named '$target_name' found in '$search_dir'"
        return 1
    end

    # Change to the directory containing the found file or folder
    cd -- (dirname "$result")
end
