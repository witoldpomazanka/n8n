docker compose down && docker images "q910705/*" -q | xargs -r docker rmi -f && git pull && docker compose up -d
