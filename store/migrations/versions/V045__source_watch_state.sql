-- 定时刷新（fetch/source_refresh.py）每个 watch 的进度：一个 watch 一行，state 是一个 JSON（这一周期的请求、在跑的
-- 采集任务、窗口检查点、上次成功和失败）。以前放在原采集库（图谱那边）的 publication_outbox 里（kind='control' 的行），
-- 抓取因此离不开第二个数据库；现在放在 History 库，抓只用这一个库。
-- 换到这张表后，各 watch 从一个新的周期开始（“遇到已存的就停”和内容指纹保证不会重复存版本）；watch_key 和以前的
-- event_key 是同一个值，要接着旧进度跑，可以把旧库 publication_outbox 里 source-watch: 开头那些行的 snapshot 照键搬过来。
CREATE TABLE IF NOT EXISTS history_source_watches (
  watch_key text PRIMARY KEY,            -- source-watch:<这条配置的 sha256>：改了配置就是一个新的 watch
  watch_id text NOT NULL,                -- 配置里写的 id，查表时看着方便
  state jsonb NOT NULL DEFAULT '{}'::jsonb,
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
