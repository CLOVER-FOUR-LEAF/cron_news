-- 定时资讯 数据库初始化脚本（MySQL / MariaDB / PostgreSQL 兼容说明见下）
-- 适用于独立数据库模式（sql/init.sql 仅在外部数据库上手工初始化时使用；
-- 项目内建 SQLite 数据库会在首次启动时自动建表，无需执行本脚本。）

CREATE DATABASE IF NOT EXISTS cron_news DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

USE cron_news;

-- ============ 分类表 ============
CREATE TABLE IF NOT EXISTS categories (
    id INT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',
    name VARCHAR(50) NOT NULL UNIQUE COMMENT '分类名称',
    sort_order INT DEFAULT 0 COMMENT '排序权重',
    is_default TINYINT(1) DEFAULT 0 COMMENT '是否为默认分类',
    color VARCHAR(20) DEFAULT NULL COMMENT '主题色',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='新闻分类';

-- ============ 新闻表 ============
CREATE TABLE IF NOT EXISTS news (
    id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',
    title VARCHAR(255) NOT NULL COMMENT '新闻标题',
    summary TEXT COMMENT '内容概要',
    content LONGTEXT COMMENT 'Markdown正文',
    source_url VARCHAR(500) COMMENT '原文链接',
    source VARCHAR(100) COMMENT '来源',
    category_id INT DEFAULT NULL COMMENT '分类ID',
    cover_url VARCHAR(500) COMMENT '封面图URL',
    collected_at DATETIME COMMENT '收录时间',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    is_deleted TINYINT DEFAULT 0 COMMENT '逻辑删除(0:正常, 1:已删除)',
    is_read TINYINT DEFAULT 0 COMMENT '已读(0/1)',
    is_reading TINYINT DEFAULT 0 COMMENT '在读(0/1)',
    reading_at DATETIME COMMENT '开始阅读时间',
    read_at DATETIME COMMENT '阅读时间',
    is_fav TINYINT DEFAULT 0 COMMENT '收藏(0/1)',
    fav_at DATETIME COMMENT '收藏时间',
    is_later TINYINT DEFAULT 0 COMMENT '稍后再读(0/1)',
    later_at DATETIME COMMENT '稍后再读标记时间',
    related_ids TEXT COMMENT '智能推荐相关新闻ID集合(JSON)',
    INDEX idx_collected_at (collected_at),
    INDEX idx_category_id (category_id),
    INDEX idx_is_deleted (is_deleted),
    INDEX idx_is_read (is_read),
    INDEX idx_collected (collected_at),
    CONSTRAINT fk_news_category FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='新闻表';

-- ============ 每日简报表（含配套便签与来源） ============
CREATE TABLE IF NOT EXISTS briefs (
    id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',
    category_name VARCHAR(50) NOT NULL COMMENT '分类名称',
    brief_date VARCHAR(10) NOT NULL COMMENT '简报日期(YYYY-MM-DD)',
    content LONGTEXT COMMENT '简报Markdown内容',
    note LONGTEXT COMMENT '配套便签',
    source VARCHAR(10) DEFAULT '自主' COMMENT '简报来源(自主/外部)',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '生成时间',
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    UNIQUE KEY uk_brief_cat_date (category_name, brief_date),
    INDEX idx_brief_date (brief_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='每日简报';

-- ============ Agent 运行日志表 ============
CREATE TABLE IF NOT EXISTS agent_runs (
    id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',
    started_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '运行开始时间',
    status VARCHAR(20) DEFAULT 'finished' COMMENT '运行状态',
    content LONGTEXT COMMENT '日志行JSON'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Agent运行日志';

-- ============ Agent 系统提示词表 ============
CREATE TABLE IF NOT EXISTS agent_prompts (
    id INT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',
    name VARCHAR(30) NOT NULL UNIQUE COMMENT 'Agent名称(timed/recommend/brief)',
    content LONGTEXT COMMENT '自定义系统提示词(空则用内置默认)'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Agent系统提示词';
