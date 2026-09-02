# A/B/C/D 入库分级 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在招聘内容进入运营岗位库前保存可审计的 A/B/C/D 学生适配分级。

**Architecture:** 硬规则先输出 D；否则模型输出受限 JSON，失败则 C。分级字段随岗位保存，并决定入库、复核或过滤留档动作。

**Tech Stack:** FastAPI、SQLAlchemy、Qwen API、pytest。
