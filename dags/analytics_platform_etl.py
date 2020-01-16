from airflow import DAG

from airflow.operators.dummy_operator import DummyOperator
from airflow.operators.python_operator import PythonOperator
from airflow.hooks import PostgresHook

from datetime import timedelta, datetime

import pprint

def failure_callback(ctx):
    print("FAILED")
    # Add PD

dag = DAG("analytics_platform_etl",
          description="Take the data from logs.analytics_platform_event and put them into the correct tables",
          schedule_interval=timedelta(minutes=30),
          concurrency=5,
          dagrun_timeout=timedelta(minutes=60),
          max_active_runs=1,
          catchup=False,
          start_date=datetime(2020, 1, 13),
          on_failure_callback=failure_callback)

def select_analytics_events(ts, conn_id, **kwargs):
    pg_hook = PostgresHook(conn_id)

    query = '''
        SELECT
            sessionId, entityId, createdAt, eventName, eventValue, userType, appVersion, metadata
        FROM
            logs.analytics_platform_event
        WHERE
            createdAt >= timestamp '%s' - interval '30 minutes'
    ''' % ts

    records = pg_hook.get_records(query)

    # Zip field names into each record
    l = [
        dict(zip(['sessionId', 'entityId', 'createdAt', 'eventName', 'eventValue', 'userType', 'appVersion', 'metadata'], r))
        for r in records
    ]
    return l

select_analytics_platform_events_task = PythonOperator(
    task_id="select_analytics_platform_events",
    provide_context=True,
    op_kwargs={'conn_id': 'analytics_redshift'},
    python_callable=select_analytics_events,
    dag=dag
)


def process_records(**kwargs):
    task_instance = kwargs['task_instance']
    records = task_instance.xcom_pull(task_ids='select_analytics_platform_events')

    for record in records:
        event_name = record['eventName']
        event_name_list = event_name.split('.')

        if len(event_name_list) < 2:
            print("EventName for record invalid, skipping record")
            pprint.pprint(record)
            continue

        if event_name_list[-1] == 'exposure':
            event_name_list[0] = 'exposure'
            event_name_list = event_name_list[:-1]

        record['logType'] = event_name_list[0] # have to do this to fix
        record['tableName'] = event_name_list[1]
        record['qualifier'] = '.'.join(event_name_list[2:]) # because of magic, this doesn't get us an index error if len < 2

    return records


process_records_task = PythonOperator(
    task_id="process_records",
    provide_context=True,
    python_callable=process_records,
    dag=dag
)


def extract_table_names(**kwargs):
    task_instance = kwargs['task_instance']
    records = task_instance.xcom_pull(task_ids='select_analytics_platform_events')
    tablename_set = set()

    for record in records:
        event_name = record['eventName']
        event_name_list = event_name.split('.')
        if len(event_name_list) < 2:
            print("EventName for record invalid, skipping record")
            pprint.pprint(record)
            continue

        tablename_set.add(event_name_list[1])

    return list(tablename_set)

extract_table_names_task = PythonOperator(
    task_id="extract_table_names",
    provide_context=True,
    python_callable=extract_table_names,
    dag=dag
)

def create_experiment_tables(**kwargs):
    task_instance = kwargs['task_instance']
    tablenames = task_instance.xcom_pull(task_ids='extract_table_names')

    def create_table(conn_id, tablename, **kwargs):
        pg_hook = PostgresHook(conn_id)
        query = '''
            CREATE TABLE IF NOT EXISTS %s (
                sessionId varchar(100) encode zstd,
                entityId varchar(100) encode zstd,
                createdAt timestamp,
                logType varchar(100) encode zstd,
                qualifier varchar(100) NULL encode zstd,
                eventValue varchar(100) NULL encode zstd,
                userType varchar(32) NULL encode zstd,
                appVersion varchar(20) NULL encode zstd,
                metadata varchar(5000) NULL encode zstd
            )
            diststyle key
            sortkey(createdAt, logType, qualifier, entityId)
        ''' % tablename
        pg_hook.run(sql)

    def create_metadata_row(conn_id, tablename, ts, **kwargs):
        pg_hook = PostgresHook(conn_id)
        query = '''
            SELECT * FROM analytics_platform.analytics_platform_metadata WHERE tableName = %s
        ''' % tablename

        records = pg_hook.get_records(query)
        if not records:
            query = '''
                INSERT INTO analytics_platform.analytics_platform_metadata (
                    tableName, createdAt, teamOwner, active, lastUpdated
                ) VALUES (
                    %s, %s, NULL, true, NULL
                )
            ''' % (tablename, ts)

        pg_hook.run(query)

    for tablename in tablenames:
        t = PythonOperator(
            task_id="create_%s_table" % tablename,
            provide_context=True,
            op_kwargs={'conn_id': 'analytics_redshift', 'tablename': tablename},
            python_callable=create_table,
            dag=dag
        )

        t1 = PythonOperator(
            task_id="insert_%s_into_metadata" % tablename,
            provide_context=True,
            op_kwargs={'conn_id': 'analytics_redshift', 'tablename': tablename},
            python_callable=create_metadata_row,
            dag=dag
        )

        # TODO: make sure to add downstream events here
        create_experiment_tables_task >> [t, t1] >> insert_records_task

create_experiment_tables_task = PythonOperator(
    task_id = 'create_experiment_tables',
    provide_context=True,
    python_callable=create_experiment_tables,
    dag=dag
)

def insert_records(conn_id, **kwargs):
    task_instance = kwargs['task_instance']
    records = task_instance.xcom_pull(task_ids='process_records')
    insert_values_by_table = {}
    query = "begin;"

    for record in records:
        insert_values = insert_values_by_table.get(record['tableName'], [])

        values = '''('%(sessionId)s','%(entityId)s',timestamp '%(createdAt)s','%(logType)s','%(qualifier)s','%(eventValue)s','%(userType)s','%(appVersion)s','%(metadata)s')''' % record
        values.replace("'None'", "NULL") # Make sure we get rid of Nones
        insert_values.append(values)

        insert_values_by_table[record['tableName']] = insert_values

    for tablename,insert_values in insert_values_by_table.items():
        partial_query = '''
            insert into %s values %s;
        ''' % (tablename, ','.join(insert_values))
        pprint.pprint(partial_query)

        query += partial_query
        #TODO run sql query
    query += "commit;"
    pprint.pprint(query)

insert_records_task = PythonOperator(
    task_id = 'insert_records',
    provide_context=True,
    op_kwargs={'conn_id': 'analytics_redshift'},
    python_callable=insert_records,
    dag=dag
)

select_analytics_platform_events_task >> [process_records_task, extract_table_names_task]
extract_table_names_task >> create_experiment_tables_task >> insert_records_task