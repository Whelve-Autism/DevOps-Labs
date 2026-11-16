#!/bin/bash
# Extended from the course-provided monitoring.sh for Amazon Linux 2023.
set -euo pipefail
trap 'printf "Monitoring command failed at line %s\n" "$LINENO" >&2' ERR

# Read instance identity with a short-lived IMDSv2 token.
metadata=http://169.254.169.254/latest
token=$(curl -fsS --max-time 5 -X PUT "$metadata/api/token" \
  -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')
instance_id=$(curl -fsS --max-time 5 -H "X-aws-ec2-metadata-token: $token" \
  "$metadata/meta-data/instance-id")
[[ $instance_id =~ ^i-[a-f0-9]+$ ]]

memory=$(free -m | awk '/^Mem:/ {printf "%.2f%% (%s of %s MiB)", $3 * 100 / $2, $3, $2}')
processes=$(ps -e -o pid= | awk 'END {print NR}')
disk=$(df -P / | awk 'NR==2 {print $5}')
load=$(awk '{print $1, $2, $3}' /proc/loadavg)
apache_state=inactive
if systemctl is-active --quiet httpd; then
  apache_state=active
fi
http_status=$(curl -sS --max-time 5 -o /dev/null -w '%{http_code}' http://127.0.0.1/) || http_status=000

printf '\nInstance ID: %s\n' "$instance_id"
printf 'Memory utilisation: %s\nProcesses: %s\nRoot filesystem used: %s\n' "$memory" "$processes" "$disk"
printf 'Load averages (1, 5, 15 min): %s\nApache: %s\nLocal homepage HTTP status: %s\n' \
  "$load" "$apache_state" "$http_status"

# Read a bounded log window. Aggregate it without printing visitor IP addresses.
sudo -n tail -n 100 /var/log/httpd/access_log | awk '
  $9 ~ /^[1-5][0-9][0-9]$/ {requests++; if ($9 >= 500) server++; else if ($9 >= 400) client++}
  END {printf "Apache access log (last 100 lines): %d requests, %d 4xx, %d 5xx\n", requests, client, server}'
sudo -n tail -n 50 /var/log/httpd/error_log | awk '
  /:(error|crit|alert|emerg)\]/ {errors++}
  END {printf "Apache error log (last 50 lines): %d error-or-higher entries\n", errors}'

# The database lives outside the document root. Keep the latest checks for review.
printf '\nSQLite health history (latest five checks, UTC)\n'
sqlite3 -header -column /var/lib/assignment1/health.db <<SQL
INSERT INTO health_checks (instance_id, http_status, apache_state)
VALUES ('$instance_id', $http_status, '$apache_state');
SELECT checked_at, instance_id, http_status, apache_state
FROM health_checks ORDER BY id DESC LIMIT 5;
SQL

if [[ $apache_state != active || $http_status != 200 ]]; then
  printf 'Apache or the homepage failed the health check.\n' >&2
  exit 1
fi
printf '\nRemote monitoring passed.\n'
