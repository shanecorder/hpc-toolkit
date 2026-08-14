# /etc/profile.d/slurm_efficiency_notice.sh

# 1. Guard against non-interactive shells (prevents breaking scp, sftp, git, rsync)
if [[ $- == *i* ]] && [ -t 1 ]; then

    SEFF_DIR="${HOME}/.local/seff"
    REPORT_FILE="${SEFF_DIR}/yesterday_report.txt"
    STAMP_FILE="${SEFF_DIR}/.last_shown_stamp"
    TODAY=$(date +%Y-%m-%d)

    # 2. Check if a report exists
    if [ -f "$REPORT_FILE" ]; then
        LAST_SHOWN=""
        [ -f "$STAMP_FILE" ] && LAST_SHOWN=$(cat "$STAMP_FILE" 2>/dev/null)

        # 3. Display only if not shown yet today
        if [ "$LAST_SHOWN" != "$TODAY" ]; then
            echo ""
            cat "$REPORT_FILE"
            echo ""
            echo "$TODAY" > "$STAMP_FILE" 2>/dev/null
        fi
    fi
fi