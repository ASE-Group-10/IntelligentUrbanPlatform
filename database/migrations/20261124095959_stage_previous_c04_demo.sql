-- migrate:up
-- Only databases that ran the earlier #119 feature branch have this schema.
-- Move it intact before dbmate applies C04's already-merged creation migration.
do $$
begin
    if to_regclass('c04_delivery_management.planning_receipts') is not null
       and to_regclass('c04_delivery_management.command_receipts') is null then
        alter schema c04_delivery_management rename to c04_previous_demo;
    end if;
end;
$$;

-- migrate:down
do $$
begin
    if to_regnamespace('c04_previous_demo') is not null then
        alter schema c04_previous_demo rename to c04_delivery_management;
    end if;
end;
$$;
