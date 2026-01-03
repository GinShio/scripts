#!/usr/bin/env bash

source $XDG_CONFIG_HOME/workflow/.env

function deploy_key() {
    rsync -L $DOTFILES_ROOT_DIR/keys/ssh.tar.zst $tmpdir
    tar --zstd -xf ssh.tar.zst
    rm -rf ssh.tar.zst{,.ssl}
    rsync --remove-source-files * $HOME/.ssh
}

function update_key() {
    local key_name=$1
    declare -n key_items="$2"
    declare -n email_name="${key_name}_EMAIL"
    local comment="$item-${email_name}"

    for item in ${key_items[@]}; do
        local comment="$item-${email_name}"
        ssh-keygen -C "$comment" -t ed25519 -f "$PWD/$item" -N ""
    done
}

args=`getopt -l "deploy,update,tmpdir:" -a -o "duT" -- $@`
eval set -- $args
while true ; do
    case "$1" in
        -d|--deploy) deploy=1; shift;;
        -u|--update) update=1; shift;;
        -T|--tmpdir) tmpdir=$2; shift 2;;
        --) shift ; break ;;
        *) echo "Internal error!" ; exit 1 ;;
    esac
done

if [ -z "$tmpdir" ]
then tmpdir=$(mktemp -d /tmp/dotfiles-XXXXXXXXX.d)
fi
cd $tmpdir

FILENAME=${FILENAME:-ssh-$(date "+%Y")}

if [[ 0 -ne $update ]]; then
    keys=( $(compgen -v |grep '_EMAIL$') )
    keys=("${keys[@]/PERSONAL_EMAIL}")
    for keyname in ${keys[@]}; do
        key_name=${keyname%_*}
        key_lower_name=$(tr '[:upper:]' '[:lower:]' <<<"$key_name")
        items=(
          "${key_lower_name}-pri-ssh"
          "${key_lower_name}-pri-git"
          "${key_lower_name}-pub-git"
        )
        update_key $key_name items
    done
    items=(
        "personal-git"
        "personal-ssh"
    )
    update_key PERSONAL items
    chmod a-w *
    tar -cf - * |zstd -z -19 --ultra --quiet -o $FILENAME.tar.zst
    rsync --remove-source-files $FILENAME.tar.zst $DOTFILES_ROOT_DIR/keys
    cd $DOTFILES_ROOT_DIR/keys
    ln -sf $FILENAME.tar.zst ssh.tar.zst
elif [[ 0 -ne $deploy ]]; then
    deploy_key
fi
