release: python manage.py migrate --noinput && python manage.py createcachetable && python manage.py collectstatic --noinput
web: gunicorn config.wsgi:application --bind 0.0.0.0:$PORT --workers 2 --timeout 60 --access-logfile - --error-logfile -
