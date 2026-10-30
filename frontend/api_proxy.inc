# Shared by the /api/ locations in nginx.conf: strip the /api prefix and proxy
# to the backend. Docker's embedded DNS resolves "backend"; resolving per
# request lets nginx start even if the backend is briefly down.
resolver 127.0.0.11 valid=10s;
set $upstream http://backend:8000;
rewrite ^/api/(.*)$ /$1 break;
proxy_pass $upstream;

proxy_set_header Host $host;
proxy_set_header X-Real-IP $client_ip;
proxy_set_header X-Forwarded-For $client_ip;
# The TLS-terminating proxy in front sets this to https.
proxy_set_header X-Forwarded-Proto $http_x_forwarded_proto;
proxy_read_timeout 120s;
proxy_send_timeout 120s;
