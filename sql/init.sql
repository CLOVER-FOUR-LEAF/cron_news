-- 定时资讯 数据库初始化脚本

CREATE DATABASE IF NOT EXISTS cron_news DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

USE cron_news;

-- 新闻表
CREATE TABLE IF NOT EXISTS news (
    id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',
    title VARCHAR(255) NOT NULL COMMENT '新闻标题',
    summary TEXT COMMENT '内容概要',
    source_url VARCHAR(500) COMMENT '原文链接',
    source VARCHAR(100) COMMENT '来源',
    category VARCHAR(20) DEFAULT '科技' COMMENT '分类',
    cover_url VARCHAR(500) COMMENT '封面图URL',
    collected_at DATETIME COMMENT '收录时间',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    is_deleted TINYINT DEFAULT 0 COMMENT '逻辑删除(0:正常, 1:已删除)',
    INDEX idx_collected_at (collected_at),
    INDEX idx_source (source),
    INDEX idx_category (category),
    INDEX idx_is_deleted (is_deleted)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='新闻表';
