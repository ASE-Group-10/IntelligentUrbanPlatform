-- migrate:up
create schema if not exists c06_city_monitoring;
comment on schema c06_city_monitoring is
    'C06 City Monitoring (UC05): observations, City State, history and outbox.';

create schema if not exists c07_disruption_detection;
comment on schema c07_disruption_detection is
    'C07 Disruption Detection (UC06): disruptions and processed event IDs.';

-- migrate:down
drop schema if exists c07_disruption_detection;
drop schema if exists c06_city_monitoring;
