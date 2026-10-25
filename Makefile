.PHONY: help setup dev-api dev-web test lint migrate agent-build down up-local down-local

help:
	@echo "make setup     安装后端与前端依赖"
	@echo "make dev-api   本地起 API（:8000）"
	@echo "make dev-web   本地起 Web（:3000）"
	@echo "make test      后端单测 + agent 构建"
	@echo "make lint      ruff + gofmt"
	@echo "make migrate   升级数据库到最新"
	@echo "make down      停掉本地 compose 依赖"
	@echo "make up-local  用 Docker 在本地整栈启动（:8080）"
	@echo "make down-local 停掉本地整栈"

setup: 
	cd backend && python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
	cd web && npm install

dev-api:
	cd backend && uvicorn app.main:app --reload --port 8000

dev-web:
	cd web && npm run dev

test:
	cd backend && pytest -q
	cd agent && go vet ./... && go build ./...

lint:
	cd backend && ruff check app tests
	cd agent && test -z "$$(gofmt -l .)"

migrate:
	cd backend && alembic upgrade head

agent-build:
	cd agent && go build -o bin/afc-agent ./cmd/agent

# 本地只起 PG + Redis（生产用 deploy/docker-compose.yml）
up-deps:
	docker compose -f deploy/docker-compose.dev.yml up -d

down:
	docker compose -f deploy/docker-compose.dev.yml down

# 本地整栈（API + Web + PG + Redis + Caddy），入口 http://localhost:8080
up-local:
	docker compose -f deploy/docker-compose.local.yml up -d --build

down-local:
	docker compose -f deploy/docker-compose.local.yml down
