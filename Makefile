# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

CODENIB_HOME ?= $(HOME)/.codenib
CODENIB_TEMP_DIR ?= $(if $(TMPDIR),$(TMPDIR),/tmp)/codenib
CODENIB_PREBUILT_DIR ?= $(CODENIB_HOME)/prebuilt
CODENIB_RESULTS_DIR ?= $(CODENIB_HOME)/results
CODENIB_SCIP_TOOLS_DIR ?= $(CODENIB_TEMP_DIR)/scip-tools
GO_VERSION ?= 1.26.4
GO_OS ?= linux
GO_ARCH ?= amd64
GO_URL ?= https://go.dev/dl/go$(GO_VERSION).$(GO_OS)-$(GO_ARCH).tar.gz
SCIP_GO_MODULE ?= github.com/scip-code/scip-go/cmd/scip-go
SCIP_GO_VERSION ?= v0.2.7
GOPLS_VERSION ?= v0.22.0
RUST_TOOLCHAIN ?= nightly-2026-02-22
SCIP_CLANG_VERSION ?= v0.4.0
SCIP_PYTHON_VERSION ?= 0.6.6
SCIP_TYPESCRIPT_VERSION ?= 0.4.0
TYPESCRIPT_LANGUAGE_SERVER_VERSION ?= 5.3.0
TYPESCRIPT_VERSION ?= 6.0.3
YARN_VERSION ?= 1.22.22
PNPM_VERSION ?= 10.34.5
BASEDPYRIGHT_VERSION ?= 1.39.8
TY_VERSION ?= 0.0.51
PYBIND11_VERSION ?= 3.0.4
ZOEKT_MODULE ?= github.com/sourcegraph/zoekt/cmd/...
ZOEKT_VERSION ?= v0.0.0-20260617111754-2d474553a112
COURSIER_VERSION ?= 2.1.24
SCIP_JAVA_VERSION ?= 0.12.3
GRADLE_VERSION ?= 8.14.2
SBT_VERSION ?= 1.12.12
DOTNET_CHANNELS ?= 8.0 10.0
SCIP_DOTNET_VERSION ?= 0.2.14
CSHARP_LS_VERSION ?= 0.25.0
BUNDLER_VERSION ?= 2.6.9
SCIP_RUBY_VERSION ?= 0.4.7
RUBY_GEM ?= gem
RUBY_PROJECT_GEMFILE ?= .codenib/Gemfile
RUBY_PROJECT_GEMSPEC_PATH ?= ..
RUBY_PROJECT_BUNDLE_PATH ?= .codenib/vendor/bundle
SCIP_PHP_PACKAGE ?= davidrjenni/scip-php:0.0.2
COMPOSER_DOCKER_IMAGE ?= composer:2
SCIP_JDK_PACKAGE ?= openjdk-21-jdk
SCIP_JDK_COMPAT_PACKAGES ?= openjdk-11-jdk
ACTIVE_SCIP_SYSTEM_PACKAGES ?= bear build-essential clang clangd cmake curl git gzip nodejs npm pkg-config python3-dev python3-venv tar unzip $(SCIP_JDK_PACKAGE)
CORE_SYSTEM_PACKAGES ?= build-essential cmake git libre2-dev pkg-config python3-dev python3-venv zlib1g-dev
SCIP_CANDIDATE_SYSTEM_PACKAGES ?= build-essential curl git gzip libyaml-dev ruby-dev ruby-full tar unzip zlib1g-dev
SCIP_PHP_SYSTEM_PACKAGES ?= php-cli composer git unzip
JDTLS_VERSION ?= 1.58.0
JDTLS_BUILD ?= 202604151538
JDTLS_URL ?= https://download.eclipse.org/jdtls/milestones/$(JDTLS_VERSION)/jdt-language-server-$(JDTLS_VERSION)-$(JDTLS_BUILD).tar.gz
INTELEPHENSE_VERSION ?= 1.18.4
RUBY_LSP_VERSION ?= 0.26.9
KOTLIN_LSP_VERSION ?= 262.8190.0
KOTLIN_LSP_EXTENSION_VERSION ?= 0.0.5
KOTLIN_LSP_URL ?= https://download-cdn.jetbrains.com/language-server/kotlin-server/$(KOTLIN_LSP_VERSION)/kotlin-server-$(KOTLIN_LSP_EXTENSION_VERSION)-linux-amd64.vsix
LSP_SMOKE_SYSTEM_PACKAGES ?= nodejs npm
SCIP_SMOKE_LANGUAGES ?= java kotlin scala csharp ruby php
SCIP_SMOKE_OUTPUT_DIR ?= $(CODENIB_TEMP_DIR)/scip-cold-start-smoke
SCIP_SMOKE_TIMEOUT ?= 300
SCIP_SMOKE_EXTRA_ARGS ?=
SCIP_PROJECT_OUTPUT_DIR ?= $(CODENIB_TEMP_DIR)/scip-project-smoke
SCIP_PROJECT_EXTRA_ARGS ?=
LSP_SMOKE_LANGUAGES ?= java csharp ruby php kotlin
LSP_SMOKE_REFERENCE_LANGUAGES ?= java
LSP_SMOKE_MIN_REFERENCES ?= java=1
LSP_SMOKE_OUTPUT_DIR ?= $(CODENIB_TEMP_DIR)/lsp-smoke
LSP_SMOKE_EXTRA_ARGS ?=
LSP_PROJECT_OUTPUT_DIR ?= $(CODENIB_TEMP_DIR)/lsp-project-smoke
LSP_PROJECT_EXTRA_ARGS ?=
GRAPH_ALIGNMENT_OUTPUT_DIR ?= $(CODENIB_TEMP_DIR)/graph-route-alignment
GRAPH_ALIGNMENT_REFERENCE_ROUTE ?= lsp
GRAPH_ALIGNMENT_CANDIDATE_ROUTE ?= scip-candidate
GRAPH_ALIGNMENT_SKIP_LEVEL ?= none
GRAPH_ALIGNMENT_TARGET_DIR ?=
GRAPH_ALIGNMENT_EXCLUDE_PATTERNS ?=
GRAPH_ALIGNMENT_EXTRA_ARGS ?=
LARGE_SCIP_PROFILE_MANIFEST ?= scripts/profiling/large_scip_repos.yml
LARGE_SCIP_PROFILE_CACHE_ROOT ?= $(CODENIB_TEMP_DIR)/large-scip-repos
LARGE_SCIP_PROFILE_OUTPUT_DIR ?= $(CODENIB_TEMP_DIR)/large-scip-profile
LARGE_SCIP_PROFILE_TIMEOUT ?= 1800
LARGE_SCIP_PROFILE_EXTRA_ARGS ?=
FACT_BUFFER_PROFILE_INDEX ?=
FACT_BUFFER_PROFILE_LANGUAGE ?=
FACT_BUFFER_PROFILE_PROJECT_ROOT ?=
FACT_BUFFER_PROFILE_OUTPUT ?=
FACT_BUFFER_PROFILE_EXTRA_ARGS ?=
PROJECT_LANGUAGE ?=
PROJECT_ROOT ?=
hash := \#
CODENIB_TOOL_PATH = $(CODENIB_SCIP_TOOLS_DIR)/gradle-$(GRADLE_VERSION)/bin:$(CODENIB_SCIP_TOOLS_DIR)/dotnet-tools:$(CODENIB_SCIP_TOOLS_DIR)/dotnet:$(CODENIB_SCIP_TOOLS_DIR)/go-tools/bin:$(CODENIB_SCIP_TOOLS_DIR)/go/bin:$(CODENIB_SCIP_TOOLS_DIR)/node-tools/node_modules/.bin:$(CODENIB_SCIP_TOOLS_DIR):$(CODENIB_SCIP_TOOLS_DIR)/gems/bin
CODENIB_TOOL_ENV = CODENIB_HOME="$(CODENIB_HOME)" CODENIB_TEMP_DIR="$(CODENIB_TEMP_DIR)" CODENIB_PREBUILT_DIR="$(CODENIB_PREBUILT_DIR)" CODENIB_RESULTS_DIR="$(CODENIB_RESULTS_DIR)" CODENIB_SCIP_TOOLS_DIR="$(CODENIB_SCIP_TOOLS_DIR)" DOTNET_ROOT="$(CODENIB_SCIP_TOOLS_DIR)/dotnet" GEM_HOME="$(CODENIB_SCIP_TOOLS_DIR)/gems" GEM_PATH="$(CODENIB_SCIP_TOOLS_DIR)/gems" GOBIN="$(CODENIB_SCIP_TOOLS_DIR)/go-tools/bin" GOPATH="$(CODENIB_SCIP_TOOLS_DIR)/go-tools" CODENIB_PHP_COMPOSER_IMAGE="$(COMPOSER_DOCKER_IMAGE)" PATH="$(CODENIB_TOOL_PATH):$$PATH"
SCIP_COLD_START_SYSTEM_PACKAGES = $(sort $(SCIP_JDK_PACKAGE) $(SCIP_CANDIDATE_SYSTEM_PACKAGES) $(SCIP_PHP_SYSTEM_PACKAGES))
MULTILANG_SYSTEM_PACKAGES = $(sort $(ACTIVE_SCIP_SYSTEM_PACKAGES) $(CORE_SYSTEM_PACKAGES) $(SCIP_JDK_PACKAGE) $(SCIP_CANDIDATE_SYSTEM_PACKAGES) $(SCIP_PHP_SYSTEM_PACKAGES) $(LSP_SMOKE_SYSTEM_PACKAGES))

define write-ruby-bundle-wrapper
	@ruby_cmd="$$(GEM_HOME="$(CODENIB_SCIP_TOOLS_DIR)/gems" GEM_PATH="$(CODENIB_SCIP_TOOLS_DIR)/gems" "$(RUBY_GEM)" environment | sed -n 's/^  - RUBY EXECUTABLE: //p')"; \
	ruby_bindir="$$(dirname "$$ruby_cmd")"; \
	bundle_exe="$$ruby_bindir/bundle"; \
	if [ ! -f "$$bundle_exe" ]; then bundle_exe="$(CODENIB_SCIP_TOOLS_DIR)/gems/gems/bundler-$(BUNDLER_VERSION)/exe/bundle"; fi; \
	test -n "$$ruby_cmd" || { echo "Could not resolve Ruby executable from $(RUBY_GEM)" >&2; exit 1; }; \
	test -x "$$ruby_cmd" || { echo "Resolved Ruby is not executable: $$ruby_cmd" >&2; exit 1; }; \
	test -f "$$bundle_exe" || { echo "Missing Bundler executable: $$bundle_exe" >&2; exit 1; }; \
	{ \
		printf '%s\n' '#!/usr/bin/env sh'; \
		printf '%s\n' 'set -eu'; \
		printf '%s\n' "ruby_cmd='$$ruby_cmd'"; \
		printf '%s\n' "ruby_bindir='$$ruby_bindir'"; \
		printf '%s\n' "bundle_exe='$$bundle_exe'"; \
		printf '%s\n' 'export GEM_HOME="$${GEM_HOME:-$(CODENIB_SCIP_TOOLS_DIR)/gems}"'; \
		printf '%s\n' 'export GEM_PATH="$${GEM_PATH:-$(CODENIB_SCIP_TOOLS_DIR)/gems}"'; \
		printf '%s\n' 'export PATH="$$ruby_bindir:$$PATH"'; \
		printf '%s\n' 'exec "$$ruby_cmd" "$$bundle_exe" "$$@"'; \
	} > "$(CODENIB_SCIP_TOOLS_DIR)/bundle"; \
	chmod +x "$(CODENIB_SCIP_TOOLS_DIR)/bundle"
endef

define write-ruby-lsp-wrapper
	@ruby_cmd="$$(GEM_HOME="$(CODENIB_SCIP_TOOLS_DIR)/gems" GEM_PATH="$(CODENIB_SCIP_TOOLS_DIR)/gems" "$(RUBY_GEM)" environment | sed -n 's/^  - RUBY EXECUTABLE: //p')"; \
	ruby_bindir="$$(dirname "$$ruby_cmd")"; \
	ruby_lsp_exe="$(CODENIB_SCIP_TOOLS_DIR)/gems/gems/ruby-lsp-$(RUBY_LSP_VERSION)/exe/ruby-lsp"; \
	test -n "$$ruby_cmd" || { echo "Could not resolve Ruby executable from $(RUBY_GEM)" >&2; exit 1; }; \
	test -x "$$ruby_cmd" || { echo "Resolved Ruby is not executable: $$ruby_cmd" >&2; exit 1; }; \
	test -f "$$ruby_lsp_exe" || { echo "Missing ruby-lsp executable: $$ruby_lsp_exe" >&2; exit 1; }; \
	{ \
		printf '%s\n' '#!/usr/bin/env sh'; \
		printf '%s\n' 'set -eu'; \
		printf '%s\n' "ruby_cmd='$$ruby_cmd'"; \
		printf '%s\n' "ruby_bindir='$$ruby_bindir'"; \
		printf '%s\n' "ruby_lsp_exe='$$ruby_lsp_exe'"; \
		printf '%s\n' 'export GEM_HOME="$${GEM_HOME:-$(CODENIB_SCIP_TOOLS_DIR)/gems}"'; \
		printf '%s\n' 'export GEM_PATH="$${GEM_PATH:-$(CODENIB_SCIP_TOOLS_DIR)/gems}"'; \
		printf '%s\n' 'export PATH="$$ruby_bindir:$$PATH"'; \
		printf '%s\n' 'exec "$$ruby_cmd" "$$ruby_lsp_exe" "$$@"'; \
	} > "$(CODENIB_SCIP_TOOLS_DIR)/ruby-lsp-bin"; \
	chmod +x "$(CODENIB_SCIP_TOOLS_DIR)/ruby-lsp-bin"
endef

GRAPH_ALIGNMENT_TOOL_TARGETS :=
SCIP_PROJECT_TOOL_TARGETS :=
ifneq ($(filter python py,$(PROJECT_LANGUAGE)),)
GRAPH_ALIGNMENT_TOOL_TARGETS := scip-python-tool python-lsp-tool
else ifneq ($(filter go golang,$(PROJECT_LANGUAGE)),)
GRAPH_ALIGNMENT_TOOL_TARGETS := go-tool scip-go-tool gopls-tool
else ifneq ($(filter rust rs,$(PROJECT_LANGUAGE)),)
GRAPH_ALIGNMENT_TOOL_TARGETS := rust-tool
else ifneq ($(filter javascript typescript js ts,$(PROJECT_LANGUAGE)),)
GRAPH_ALIGNMENT_TOOL_TARGETS := scip-typescript-tool node-workspace-tools typescript-lsp-tool
else ifneq ($(filter cpp c,$(PROJECT_LANGUAGE)),)
GRAPH_ALIGNMENT_TOOL_TARGETS := clangd-tool scip-clang-tool
else ifneq ($(filter java,$(PROJECT_LANGUAGE)),)
GRAPH_ALIGNMENT_TOOL_TARGETS := scip-java-tool gradle-tool jdtls-tool
else ifneq ($(filter csharp c$(hash),$(PROJECT_LANGUAGE)),)
GRAPH_ALIGNMENT_TOOL_TARGETS := scip-dotnet-tool csharp-lsp-tool
else ifneq ($(filter php,$(PROJECT_LANGUAGE)),)
GRAPH_ALIGNMENT_TOOL_TARGETS := intelephense-tool scip-php-info
else ifneq ($(filter ruby rb,$(PROJECT_LANGUAGE)),)
GRAPH_ALIGNMENT_TOOL_TARGETS := scip-ruby-tool ruby-lsp-tool
else ifneq ($(filter kotlin kt,$(PROJECT_LANGUAGE)),)
GRAPH_ALIGNMENT_TOOL_TARGETS := scip-java-tool gradle-tool kotlin-lsp-tool
else ifneq ($(filter scala,$(PROJECT_LANGUAGE)),)
GRAPH_ALIGNMENT_TOOL_TARGETS := scip-java-tool gradle-tool sbt-tool
else ifneq ($(strip $(PROJECT_LANGUAGE)),)
GRAPH_ALIGNMENT_TOOL_TARGETS := multilang-tools
endif
ifneq ($(filter java,$(PROJECT_LANGUAGE)),)
SCIP_PROJECT_TOOL_TARGETS := scip-java-tool gradle-tool
else ifneq ($(filter kotlin kt,$(PROJECT_LANGUAGE)),)
SCIP_PROJECT_TOOL_TARGETS := scip-java-tool gradle-tool
else ifneq ($(filter scala,$(PROJECT_LANGUAGE)),)
SCIP_PROJECT_TOOL_TARGETS := scip-java-tool gradle-tool sbt-tool
else ifneq ($(filter csharp c$(hash),$(PROJECT_LANGUAGE)),)
SCIP_PROJECT_TOOL_TARGETS := scip-dotnet-tool
else ifneq ($(filter ruby rb,$(PROJECT_LANGUAGE)),)
SCIP_PROJECT_TOOL_TARGETS := scip-ruby-tool
else ifneq ($(filter php,$(PROJECT_LANGUAGE)),)
SCIP_PROJECT_TOOL_TARGETS := scip-php-info
else ifneq ($(strip $(PROJECT_LANGUAGE)),)
SCIP_PROJECT_TOOL_TARGETS := scip-cold-start-tools
endif

define require-command
	@command -v $(1) >/dev/null 2>&1 || { echo "Missing required command: $(1)" >&2; exit 1; }
endef

.PHONY: install scip bootstrap bootstrap-ubuntu multilang-system-deps-ubuntu multilang-tools toolchain-doctor
.PHONY: active-scip-tools active-lsp-tools active-scip-env active-system-deps-ubuntu
.PHONY: go-tool scip-go-tool rust-tool scip-python-tool scip-typescript-tool scip-clang-tool
.PHONY: node-workspace-tools zoekt-tool python-lsp-tool ty-tool typescript-lsp-tool gopls-tool clangd-tool
.PHONY: core-system-deps-ubuntu core-python-deps core-build core-test fact-buffer-profile fact-query-profile clangd-fact-query-profile
.PHONY: scip-cold-start-tools scip-cold-start-tools-all scip-cold-start-env scip-cold-start-system-deps-ubuntu
.PHONY: scip-candidates scip-candidates-all scip-candidate-env scip-candidate-system-deps-ubuntu
.PHONY: scip-jvm-compat-system-deps-ubuntu
.PHONY: scip-cold-start-smoke scip-candidate-smoke scip-project-smoke-tools scip-project-smoke lsp-smoke lsp-project-smoke graph-route-alignment-tools graph-route-alignment large-scip-profile multilang-smoke multilang-registry-check
.PHONY: scip-java-tool gradle-tool sbt-tool dotnet-tool scip-dotnet-tool scip-ruby-tool
.PHONY: scip-ruby-system-deps-ubuntu ruby-project-bundle
.PHONY: scip-php-info scip-php-tool scip-php-docker-tool scip-php-system-deps-ubuntu php-project-scip-tool
.PHONY: lsp-smoke-tools lsp-smoke-env lsp-smoke-system-deps-ubuntu
.PHONY: jdtls-tool csharp-lsp-tool ruby-lsp-tool intelephense-tool kotlin-lsp-tool
.PHONY: dev test test-all test-slow ask-quality branding-assets branding-check
.PHONY: web-deps web-start web-stop web-restart web-reclaim web-status web-logs web-follow

install:
	pip install -e .

scip:
	pip install -e .
	@$(MAKE) --no-print-directory active-scip-tools

bootstrap: dev multilang-tools core-python-deps
	@$(MAKE) --no-print-directory active-scip-env

bootstrap-ubuntu: multilang-system-deps-ubuntu bootstrap

multilang-tools: active-scip-tools active-lsp-tools scip-cold-start-tools lsp-smoke-tools zoekt-tool

multilang-system-deps-ubuntu:
	$(call require-command,sudo)
	$(call require-command,apt-get)
	sudo apt-get update
	sudo apt-get install -y $(MULTILANG_SYSTEM_PACKAGES)

active-system-deps-ubuntu:
	$(call require-command,sudo)
	$(call require-command,apt-get)
	sudo apt-get update
	sudo apt-get install -y $(ACTIVE_SCIP_SYSTEM_PACKAGES)

core-system-deps-ubuntu:
	$(call require-command,sudo)
	$(call require-command,apt-get)
	sudo apt-get update
	sudo apt-get install -y $(CORE_SYSTEM_PACKAGES)

scip-jvm-compat-system-deps-ubuntu:
	$(call require-command,sudo)
	$(call require-command,apt-get)
	sudo apt-get update
	sudo apt-get install -y $(SCIP_JDK_COMPAT_PACKAGES)

active-scip-tools: scip-python-tool scip-typescript-tool node-workspace-tools scip-go-tool rust-tool scip-clang-tool scip-java-tool gradle-tool scip-dotnet-tool
	@$(MAKE) --no-print-directory active-scip-env

active-lsp-tools: python-lsp-tool ty-tool typescript-lsp-tool gopls-tool clangd-tool
	@$(MAKE) --no-print-directory active-scip-env

active-scip-env:
	@echo "CodeNib tools installed under: $(CODENIB_SCIP_TOOLS_DIR)"
	@echo "Use this environment for active SCIP/LSP and cold-start smoke runs:"
	@echo "  export CODENIB_SCIP_TOOLS_DIR=$(CODENIB_SCIP_TOOLS_DIR)"
	@echo "  export DOTNET_ROOT=$(CODENIB_SCIP_TOOLS_DIR)/dotnet"
	@echo "  export GEM_HOME=$(CODENIB_SCIP_TOOLS_DIR)/gems"
	@echo "  export GEM_PATH=$(CODENIB_SCIP_TOOLS_DIR)/gems"
	@echo "  export GOBIN=$(CODENIB_SCIP_TOOLS_DIR)/go-tools/bin"
	@echo "  export GOPATH=$(CODENIB_SCIP_TOOLS_DIR)/go-tools"
	@echo "  export CODENIB_PHP_COMPOSER_IMAGE=$(COMPOSER_DOCKER_IMAGE)"
	@echo "  export PATH=$(CODENIB_TOOL_PATH):\$$PATH"

toolchain-doctor:
	@$(CODENIB_TOOL_ENV) sh -eu -c 'missing=0; \
		for cmd in scip-python scip-typescript yarn pnpm scip-go scip-clang rust-analyzer go gopls basedpyright-langserver ty typescript-language-server clangd scip-java gradle sbt dotnet scip-dotnet csharp-ls ruby-lsp intelephense kotlin-language-server zoekt-git-index zoekt-webserver; do \
			if command -v "$$cmd" >/dev/null 2>&1; then \
				printf "ok %s -> %s\n" "$$cmd" "$$(command -v "$$cmd")"; \
			else \
				printf "missing %s\n" "$$cmd" >&2; missing=1; \
			fi; \
		done; \
		exit "$$missing"'

go-tool:
	$(call require-command,curl)
	$(call require-command,tar)
	mkdir -p "$(CODENIB_SCIP_TOOLS_DIR)"
	@if [ ! -f "$(CODENIB_SCIP_TOOLS_DIR)/go/.codenib-version" ] \
		|| ! grep -qx "$(GO_VERSION)" "$(CODENIB_SCIP_TOOLS_DIR)/go/.codenib-version"; then \
		rm -rf "$(CODENIB_SCIP_TOOLS_DIR)/go" \
			"$(CODENIB_SCIP_TOOLS_DIR)/go$(GO_VERSION).$(GO_OS)-$(GO_ARCH).tar.gz"; \
		curl -fL "$(GO_URL)" \
			-o "$(CODENIB_SCIP_TOOLS_DIR)/go$(GO_VERSION).$(GO_OS)-$(GO_ARCH).tar.gz"; \
		tar -xzf "$(CODENIB_SCIP_TOOLS_DIR)/go$(GO_VERSION).$(GO_OS)-$(GO_ARCH).tar.gz" \
			-C "$(CODENIB_SCIP_TOOLS_DIR)"; \
		echo "$(GO_VERSION)" > "$(CODENIB_SCIP_TOOLS_DIR)/go/.codenib-version"; \
	fi

scip-go-tool: go-tool
	mkdir -p "$(CODENIB_SCIP_TOOLS_DIR)/go-tools/bin"
	GOBIN="$(CODENIB_SCIP_TOOLS_DIR)/go-tools/bin" \
	GOPATH="$(CODENIB_SCIP_TOOLS_DIR)/go-tools" \
	PATH="$(CODENIB_SCIP_TOOLS_DIR)/go/bin:$$PATH" \
		"$(CODENIB_SCIP_TOOLS_DIR)/go/bin/go" install \
		"$(SCIP_GO_MODULE)@$(SCIP_GO_VERSION)"

gopls-tool: go-tool
	mkdir -p "$(CODENIB_SCIP_TOOLS_DIR)/go-tools/bin"
	GOBIN="$(CODENIB_SCIP_TOOLS_DIR)/go-tools/bin" \
	GOPATH="$(CODENIB_SCIP_TOOLS_DIR)/go-tools" \
	PATH="$(CODENIB_SCIP_TOOLS_DIR)/go/bin:$$PATH" \
		"$(CODENIB_SCIP_TOOLS_DIR)/go/bin/go" install \
		"golang.org/x/tools/gopls@$(GOPLS_VERSION)"

rust-tool:
	$(call require-command,curl)
	@if ! command -v rustup >/dev/null 2>&1; then \
		curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y; \
	fi
	PATH="$$HOME/.cargo/bin:$$PATH" rustup toolchain install "$(RUST_TOOLCHAIN)"
	PATH="$$HOME/.cargo/bin:$$PATH" rustup component add rust-analyzer --toolchain "$(RUST_TOOLCHAIN)"
	mkdir -p "$(CODENIB_SCIP_TOOLS_DIR)"
	@rust_analyzer="$$(PATH="$$HOME/.cargo/bin:$$PATH" rustup which --toolchain "$(RUST_TOOLCHAIN)" rust-analyzer)"; \
		ln -sf "$$rust_analyzer" "$(CODENIB_SCIP_TOOLS_DIR)/rust-analyzer"

scip-python-tool:
	$(call require-command,npm)
	mkdir -p "$(CODENIB_SCIP_TOOLS_DIR)/node-tools"
	npm install --prefix "$(CODENIB_SCIP_TOOLS_DIR)/node-tools" \
		"@sourcegraph/scip-python@$(SCIP_PYTHON_VERSION)"
	ln -sf "$(CODENIB_SCIP_TOOLS_DIR)/node-tools/node_modules/.bin/scip-python" \
		"$(CODENIB_SCIP_TOOLS_DIR)/scip-python"

scip-typescript-tool:
	$(call require-command,npm)
	mkdir -p "$(CODENIB_SCIP_TOOLS_DIR)/node-tools"
	npm install --prefix "$(CODENIB_SCIP_TOOLS_DIR)/node-tools" \
		"@sourcegraph/scip-typescript@$(SCIP_TYPESCRIPT_VERSION)"
	ln -sf "$(CODENIB_SCIP_TOOLS_DIR)/node-tools/node_modules/.bin/scip-typescript" \
		"$(CODENIB_SCIP_TOOLS_DIR)/scip-typescript"

node-workspace-tools:
	$(call require-command,npm)
	mkdir -p "$(CODENIB_SCIP_TOOLS_DIR)/node-tools"
	npm install --prefix "$(CODENIB_SCIP_TOOLS_DIR)/node-tools" \
		"yarn@$(YARN_VERSION)" \
		"pnpm@$(PNPM_VERSION)"
	ln -sf "$(CODENIB_SCIP_TOOLS_DIR)/node-tools/node_modules/.bin/yarn" \
		"$(CODENIB_SCIP_TOOLS_DIR)/yarn"
	ln -sf "$(CODENIB_SCIP_TOOLS_DIR)/node-tools/node_modules/.bin/pnpm" \
		"$(CODENIB_SCIP_TOOLS_DIR)/pnpm"

zoekt-tool: go-tool
	mkdir -p "$(CODENIB_SCIP_TOOLS_DIR)/go-tools/bin"
	GOBIN="$(CODENIB_SCIP_TOOLS_DIR)/go-tools/bin" \
	GOPATH="$(CODENIB_SCIP_TOOLS_DIR)/go-tools" \
	PATH="$(CODENIB_SCIP_TOOLS_DIR)/go/bin:$$PATH" \
		"$(CODENIB_SCIP_TOOLS_DIR)/go/bin/go" install \
		"$(ZOEKT_MODULE)@$(ZOEKT_VERSION)"

scip-clang-tool:
	$(call require-command,curl)
	mkdir -p "$(CODENIB_SCIP_TOOLS_DIR)"
	@os="$$(uname -s | tr '[:upper:]' '[:lower:]')"; \
	arch="$$(uname -m)"; \
	if [ "$$arch" != "x86_64" ]; then \
		echo "Unsupported scip-clang architecture: $$arch (expected x86_64)" >&2; \
		exit 1; \
	fi; \
	case "$$os" in linux|darwin) ;; *) \
		echo "Unsupported scip-clang OS: $$os (expected linux or darwin)" >&2; \
		exit 1; \
	esac; \
	bin="scip-clang-x86_64-$$os"; \
	url="https://github.com/sourcegraph/scip-clang/releases/download/$(SCIP_CLANG_VERSION)/$$bin"; \
	if [ ! -f "$(CODENIB_SCIP_TOOLS_DIR)/scip-clang.version" ] \
		|| ! grep -qx "$(SCIP_CLANG_VERSION)" "$(CODENIB_SCIP_TOOLS_DIR)/scip-clang.version"; then \
		curl -fL "$$url" -o "$(CODENIB_SCIP_TOOLS_DIR)/scip-clang"; \
		chmod +x "$(CODENIB_SCIP_TOOLS_DIR)/scip-clang"; \
		echo "$(SCIP_CLANG_VERSION)" > "$(CODENIB_SCIP_TOOLS_DIR)/scip-clang.version"; \
	fi

python-lsp-tool:
	$(call require-command,npm)
	mkdir -p "$(CODENIB_SCIP_TOOLS_DIR)/node-tools"
	npm install --prefix "$(CODENIB_SCIP_TOOLS_DIR)/node-tools" \
		"basedpyright@$(BASEDPYRIGHT_VERSION)"
	ln -sf "$(CODENIB_SCIP_TOOLS_DIR)/node-tools/node_modules/.bin/basedpyright-langserver" \
		"$(CODENIB_SCIP_TOOLS_DIR)/basedpyright-langserver"

ty-tool:
	$(call require-command,python)
	python -m venv "$(CODENIB_SCIP_TOOLS_DIR)/python-tools"
	"$(CODENIB_SCIP_TOOLS_DIR)/python-tools/bin/python" -m pip install \
		--upgrade pip "ty==$(TY_VERSION)"
	ln -sf "$(CODENIB_SCIP_TOOLS_DIR)/python-tools/bin/ty" \
		"$(CODENIB_SCIP_TOOLS_DIR)/ty"

typescript-lsp-tool:
	$(call require-command,npm)
	mkdir -p "$(CODENIB_SCIP_TOOLS_DIR)/node-tools"
	npm install --prefix "$(CODENIB_SCIP_TOOLS_DIR)/node-tools" \
		"typescript-language-server@$(TYPESCRIPT_LANGUAGE_SERVER_VERSION)" \
		"typescript@$(TYPESCRIPT_VERSION)"
	ln -sf "$(CODENIB_SCIP_TOOLS_DIR)/node-tools/node_modules/.bin/typescript-language-server" \
		"$(CODENIB_SCIP_TOOLS_DIR)/typescript-language-server"

clangd-tool:
	@if ! command -v clangd >/dev/null 2>&1; then \
		echo "Missing clangd. On Ubuntu, run: make active-system-deps-ubuntu" >&2; \
		exit 1; \
	fi
	mkdir -p "$(CODENIB_SCIP_TOOLS_DIR)"
	ln -sf "$$(command -v clangd)" "$(CODENIB_SCIP_TOOLS_DIR)/clangd"

core-python-deps:
	python -m pip install "pybind11==$(PYBIND11_VERSION)"

core-build: core-python-deps
	cmake -S core -B build/core
	cmake --build build/core

core-test: core-build
	./build/core/scip_decode_test
	./build/core/graph_layers_test
	./build/core/fact_batch_buffer_test
	./build/core/content_digest_test
	./build/core/fact_query_index_test
	PYTHONPATH="build/core:$$PYTHONPATH" python -c 'import sys; import codenib_core as core; required = ("decode_clangd_fact_query_index", "clangd_fact_query_contract", "clangd_fact_query_snapshot"); missing = [name for name in required if not hasattr(core, name)]; missing and sys.exit(f"codenib_core missing required bindings: {missing}")'
	PYTHONPATH="build/core:$$PYTHONPATH" python -m pytest -q \
		test/scip/test_scip_core.py \
		test/scip/test_scip_core_registry.py \
		test/scip/test_fact_batch_buffer.py \
		test/scip/test_fact_query_index.py \
		test/facts/test_model.py \
		test/facts/test_adapters.py \
		test/scripts/test_profile_fact_batch_buffer.py \
		test/scripts/test_profile_fact_query_index.py \
		test/ls_index/test_clangd_fact_query.py

fact-buffer-profile: core-build
	@test -n "$(FACT_BUFFER_PROFILE_INDEX)" || { echo "Set FACT_BUFFER_PROFILE_INDEX=/path/to/index.decoded" >&2; exit 1; }
	@test -n "$(FACT_BUFFER_PROFILE_LANGUAGE)" || { echo "Set FACT_BUFFER_PROFILE_LANGUAGE=<language>" >&2; exit 1; }
	PYTHONPATH="build/core:$$PYTHONPATH" python scripts/profiling/profile_fact_batch_buffer.py \
		--index "$(FACT_BUFFER_PROFILE_INDEX)" \
		--language "$(FACT_BUFFER_PROFILE_LANGUAGE)" \
		$(if $(FACT_BUFFER_PROFILE_PROJECT_ROOT),--project-root "$(FACT_BUFFER_PROFILE_PROJECT_ROOT)",) \
		$(if $(FACT_BUFFER_PROFILE_OUTPUT),--output-json "$(FACT_BUFFER_PROFILE_OUTPUT)",) \
		$(FACT_BUFFER_PROFILE_EXTRA_ARGS)

fact-query-profile: core-build
	@test -n "$(FACT_QUERY_PROFILE_INDEX)" || { echo "Set FACT_QUERY_PROFILE_INDEX=/path/to/index.decoded" >&2; exit 1; }
	@test -n "$(FACT_QUERY_PROFILE_LANGUAGE)" || { echo "Set FACT_QUERY_PROFILE_LANGUAGE=<language>" >&2; exit 1; }
	PYTHONPATH="build/core:$$PYTHONPATH" python scripts/profiling/profile_fact_query_index.py \
		--index "$(FACT_QUERY_PROFILE_INDEX)" \
		--language "$(FACT_QUERY_PROFILE_LANGUAGE)" \
		$(if $(FACT_QUERY_PROFILE_PROJECT_ROOT),--project-root "$(FACT_QUERY_PROFILE_PROJECT_ROOT)",) \
		$(if $(FACT_QUERY_PROFILE_OUTPUT),--output-json "$(FACT_QUERY_PROFILE_OUTPUT)",) \
		$(FACT_QUERY_PROFILE_EXTRA_ARGS)

clangd-fact-query-profile: core-build
	@test -n "$(CLANGD_FACT_QUERY_PROFILE_INDEX_DIR)" || { echo "Set CLANGD_FACT_QUERY_PROFILE_INDEX_DIR=/path/to/.cache/clangd/index" >&2; exit 1; }
	@test -n "$(CLANGD_FACT_QUERY_PROFILE_PROJECT_ROOT)" || { echo "Set CLANGD_FACT_QUERY_PROFILE_PROJECT_ROOT=/path/to/repository" >&2; exit 1; }
	PYTHONPATH="build/core:$$PYTHONPATH" python scripts/profiling/profile_clangd_fact_query_index.py \
		--idx-directory "$(CLANGD_FACT_QUERY_PROFILE_INDEX_DIR)" \
		--project-root "$(CLANGD_FACT_QUERY_PROFILE_PROJECT_ROOT)" \
		$(if $(CLANGD_FACT_QUERY_PROFILE_OUTPUT),--output-json "$(CLANGD_FACT_QUERY_PROFILE_OUTPUT)",) \
		$(CLANGD_FACT_QUERY_PROFILE_EXTRA_ARGS)

scip-cold-start-tools: scip-java-tool gradle-tool sbt-tool scip-dotnet-tool scip-ruby-tool scip-php-info
	@$(MAKE) --no-print-directory scip-cold-start-env

scip-candidates: scip-cold-start-tools

scip-cold-start-tools-all: scip-java-tool gradle-tool sbt-tool scip-dotnet-tool scip-ruby-tool scip-php-tool
	@$(MAKE) --no-print-directory scip-cold-start-env

scip-candidates-all: scip-cold-start-tools-all

scip-cold-start-env:
	@echo "SCIP cold-start tools installed under: $(CODENIB_SCIP_TOOLS_DIR)"
	@echo "Use this environment for active Java/Kotlin/Scala/C#/Ruby/PHP smoke runs:"
	@echo "  export CODENIB_SCIP_TOOLS_DIR=$(CODENIB_SCIP_TOOLS_DIR)"
	@echo "  export DOTNET_ROOT=$(CODENIB_SCIP_TOOLS_DIR)/dotnet"
	@echo "  export GEM_HOME=$(CODENIB_SCIP_TOOLS_DIR)/gems"
	@echo "  export GEM_PATH=$(CODENIB_SCIP_TOOLS_DIR)/gems"
	@echo "  export GOBIN=$(CODENIB_SCIP_TOOLS_DIR)/go-tools/bin"
	@echo "  export GOPATH=$(CODENIB_SCIP_TOOLS_DIR)/go-tools"
	@echo "  export CODENIB_PHP_COMPOSER_IMAGE=$(COMPOSER_DOCKER_IMAGE)"
	@echo "  export PATH=$(CODENIB_TOOL_PATH):\$$PATH"

scip-candidate-env: scip-cold-start-env

scip-cold-start-smoke: scip-cold-start-tools
	$(CODENIB_TOOL_ENV) python scripts/smoke_scip_cold_start.py \
		--languages $(SCIP_SMOKE_LANGUAGES) \
		--output-dir "$(SCIP_SMOKE_OUTPUT_DIR)" \
		--timeout "$(SCIP_SMOKE_TIMEOUT)" \
		--json $(SCIP_SMOKE_EXTRA_ARGS)

scip-candidate-smoke: scip-cold-start-smoke

scip-project-smoke-tools: $(SCIP_PROJECT_TOOL_TARGETS)
	@test -n "$(PROJECT_LANGUAGE)" || { echo "Set PROJECT_LANGUAGE=<language>" >&2; exit 1; }
	@echo "SCIP project smoke tools for $(PROJECT_LANGUAGE): $(SCIP_PROJECT_TOOL_TARGETS)"

scip-project-smoke: scip-project-smoke-tools
	@test -n "$(PROJECT_LANGUAGE)" || { echo "Set PROJECT_LANGUAGE=<language>" >&2; exit 1; }
	@test -n "$(PROJECT_ROOT)" || { echo "Set PROJECT_ROOT=/path/to/project" >&2; exit 1; }
	$(CODENIB_TOOL_ENV) python scripts/smoke_scip_cold_start.py \
		--languages "$(PROJECT_LANGUAGE)" \
		--project-root "$(PROJECT_ROOT)" \
		--output-dir "$(SCIP_PROJECT_OUTPUT_DIR)" \
		--timeout "$(SCIP_SMOKE_TIMEOUT)" \
		--json $(SCIP_PROJECT_EXTRA_ARGS)

scip-cold-start-system-deps-ubuntu:
	$(call require-command,sudo)
	$(call require-command,apt-get)
	sudo apt-get update
	sudo apt-get install -y $(SCIP_COLD_START_SYSTEM_PACKAGES)

scip-candidate-system-deps-ubuntu: scip-cold-start-system-deps-ubuntu

scip-java-tool:
	$(call require-command,curl)
	$(call require-command,gzip)
	@if ! command -v java >/dev/null 2>&1; then \
		echo "Missing java. On Ubuntu, run: make scip-cold-start-system-deps-ubuntu" >&2; \
		exit 1; \
	fi
	mkdir -p "$(CODENIB_SCIP_TOOLS_DIR)"
	@if [ ! -x "$(CODENIB_SCIP_TOOLS_DIR)/coursier" ]; then \
		curl -fL "https://github.com/coursier/coursier/releases/download/v$(COURSIER_VERSION)/cs-x86_64-pc-linux.gz" \
			| gzip -dc > "$(CODENIB_SCIP_TOOLS_DIR)/coursier"; \
		chmod +x "$(CODENIB_SCIP_TOOLS_DIR)/coursier"; \
	fi
	COURSIER_CACHE="$(CODENIB_SCIP_TOOLS_DIR)/coursier-cache" \
		"$(CODENIB_SCIP_TOOLS_DIR)/coursier" bootstrap \
		"com.sourcegraph:scip-java_2.13:$(SCIP_JAVA_VERSION)" \
		-M com.sourcegraph.scip_java.ScipJava \
		-o "$(CODENIB_SCIP_TOOLS_DIR)/scip-java" -f

gradle-tool:
	$(call require-command,curl)
	$(call require-command,unzip)
	mkdir -p "$(CODENIB_SCIP_TOOLS_DIR)"
	@if [ ! -x "$(CODENIB_SCIP_TOOLS_DIR)/gradle-$(GRADLE_VERSION)/bin/gradle" ]; then \
		curl -fL "https://services.gradle.org/distributions/gradle-$(GRADLE_VERSION)-bin.zip" \
			-o "$(CODENIB_SCIP_TOOLS_DIR)/gradle-$(GRADLE_VERSION)-bin.zip"; \
		unzip -q -o "$(CODENIB_SCIP_TOOLS_DIR)/gradle-$(GRADLE_VERSION)-bin.zip" \
			-d "$(CODENIB_SCIP_TOOLS_DIR)"; \
	fi

sbt-tool: scip-java-tool
	COURSIER_CACHE="$(CODENIB_SCIP_TOOLS_DIR)/coursier-cache" \
		"$(CODENIB_SCIP_TOOLS_DIR)/coursier" install \
		"sbt:$(SBT_VERSION)" \
		--install-dir "$(CODENIB_SCIP_TOOLS_DIR)" \
		--force

dotnet-tool:
	$(call require-command,curl)
	mkdir -p "$(CODENIB_SCIP_TOOLS_DIR)"
	@if [ ! -x "$(CODENIB_SCIP_TOOLS_DIR)/dotnet-install.sh" ]; then \
		curl -fL https://dot.net/v1/dotnet-install.sh \
			-o "$(CODENIB_SCIP_TOOLS_DIR)/dotnet-install.sh"; \
		chmod +x "$(CODENIB_SCIP_TOOLS_DIR)/dotnet-install.sh"; \
	fi
	@for channel in $(DOTNET_CHANNELS); do \
		"$(CODENIB_SCIP_TOOLS_DIR)/dotnet-install.sh" \
			--channel "$$channel" \
			--install-dir "$(CODENIB_SCIP_TOOLS_DIR)/dotnet" \
			--no-path; \
	done

scip-dotnet-tool: dotnet-tool
	mkdir -p "$(CODENIB_SCIP_TOOLS_DIR)/dotnet-tools"
	DOTNET_ROOT="$(CODENIB_SCIP_TOOLS_DIR)/dotnet" \
	PATH="$(CODENIB_SCIP_TOOLS_DIR)/dotnet:$$PATH" \
		"$(CODENIB_SCIP_TOOLS_DIR)/dotnet/dotnet" tool update \
		--tool-path "$(CODENIB_SCIP_TOOLS_DIR)/dotnet-tools" \
		scip-dotnet --version "$(SCIP_DOTNET_VERSION)" \
		|| DOTNET_ROOT="$(CODENIB_SCIP_TOOLS_DIR)/dotnet" \
			PATH="$(CODENIB_SCIP_TOOLS_DIR)/dotnet:$$PATH" \
			"$(CODENIB_SCIP_TOOLS_DIR)/dotnet/dotnet" tool install \
			--tool-path "$(CODENIB_SCIP_TOOLS_DIR)/dotnet-tools" \
			scip-dotnet --version "$(SCIP_DOTNET_VERSION)"
	DOTNET_ROOT="$(CODENIB_SCIP_TOOLS_DIR)/dotnet" \
	PATH="$(CODENIB_SCIP_TOOLS_DIR)/dotnet:$$PATH" \
		"$(CODENIB_SCIP_TOOLS_DIR)/dotnet/dotnet" tool update \
		--tool-path "$(CODENIB_SCIP_TOOLS_DIR)/dotnet-tools" \
		csharp-ls --version "$(CSHARP_LS_VERSION)" \
		|| DOTNET_ROOT="$(CODENIB_SCIP_TOOLS_DIR)/dotnet" \
			PATH="$(CODENIB_SCIP_TOOLS_DIR)/dotnet:$$PATH" \
			"$(CODENIB_SCIP_TOOLS_DIR)/dotnet/dotnet" tool install \
			--tool-path "$(CODENIB_SCIP_TOOLS_DIR)/dotnet-tools" \
			csharp-ls --version "$(CSHARP_LS_VERSION)"

scip-ruby-system-deps-ubuntu:
	$(call require-command,sudo)
	$(call require-command,apt-get)
	sudo apt-get update
	sudo apt-get install -y build-essential libyaml-dev ruby-dev ruby-full zlib1g-dev

scip-ruby-tool:
	@if ! command -v "$(RUBY_GEM)" >/dev/null 2>&1; then \
		echo "Missing $(RUBY_GEM). On Ubuntu, run: make scip-ruby-system-deps-ubuntu" >&2; \
		exit 1; \
	fi
	mkdir -p "$(CODENIB_SCIP_TOOLS_DIR)/gems"
	GEM_HOME="$(CODENIB_SCIP_TOOLS_DIR)/gems" \
	GEM_PATH="$(CODENIB_SCIP_TOOLS_DIR)/gems" \
		"$(RUBY_GEM)" install bundler -v "$(BUNDLER_VERSION)" --no-document
	GEM_HOME="$(CODENIB_SCIP_TOOLS_DIR)/gems" \
	GEM_PATH="$(CODENIB_SCIP_TOOLS_DIR)/gems" \
		"$(RUBY_GEM)" install scip-ruby -v "$(SCIP_RUBY_VERSION)" --no-document
	$(write-ruby-bundle-wrapper)

ruby-project-bundle: ruby-lsp-tool scip-ruby-tool
	@test -n "$(PROJECT_ROOT)" || { echo "Set PROJECT_ROOT=/path/to/ruby/project" >&2; exit 1; }
	@$(CODENIB_TOOL_ENV) sh -eu -c 'project_root="$$(cd "$(PROJECT_ROOT)" && pwd)"; \
		gemfile="$(RUBY_PROJECT_GEMFILE)"; \
		case "$$gemfile" in /*) ;; *) gemfile="$$project_root/$$gemfile";; esac; \
		mkdir -p "$$(dirname "$$gemfile")"; \
		if [ ! -f "$$gemfile" ]; then \
			{ \
				printf "%s\n" "source \"https://rubygems.org\""; \
				printf "%s\n" ""; \
				printf "%s\n" "gemspec path: \"$(RUBY_PROJECT_GEMSPEC_PATH)\""; \
				printf "%s\n" "gem \"ruby-lsp\", \"$(RUBY_LSP_VERSION)\""; \
				printf "%s\n" "gem \"scip-ruby\", \"$(SCIP_RUBY_VERSION)\""; \
			} > "$$gemfile"; \
		fi; \
		cd "$$project_root"; \
		unset GEM_PATH; \
		BUNDLE_GEMFILE="$$gemfile" bundle config set path "$(RUBY_PROJECT_BUNDLE_PATH)"; \
		BUNDLE_GEMFILE="$$gemfile" bundle install; \
		printf "export CODENIB_RUBY_BUNDLE_GEMFILE=%s\n" "$$gemfile"'

scip-php-info:
	@echo "scip-php is project-local. In the target PHP repo, run:"
	@echo "  composer require --dev $(SCIP_PHP_PACKAGE)"
	@echo "  composer install"
	@echo "  vendor/bin/scip-php"
	@echo "CodeNib route gates prepare scip-php in an output-local worktree by default;"
	@echo "use php-project-scip-tool only when you intentionally prewarm a disposable checkout."
	@echo "Generated PHP smoke can also use Docker with:"
	@echo "  make scip-php-docker-tool"
	@echo "  CODENIB_PHP_COMPOSER_IMAGE=$(COMPOSER_DOCKER_IMAGE)"

scip-php-tool:
	@if ! command -v php >/dev/null 2>&1; then \
		echo "Missing php. On Ubuntu, run: make scip-php-system-deps-ubuntu" >&2; \
		exit 1; \
	fi
	@php -r 'exit(PHP_VERSION_ID >= 80200 ? 0 : 1);' || { \
		echo "scip-php requires PHP >= 8.2. Override SCIP_PHP_SYSTEM_PACKAGES or use make scip-php-docker-tool." >&2; \
		exit 1; \
	}
	@if ! command -v composer >/dev/null 2>&1 && [ ! -x "$(CODENIB_SCIP_TOOLS_DIR)/composer" ]; then \
		command -v curl >/dev/null 2>&1 || { echo "Missing required command: curl" >&2; exit 1; }; \
		mkdir -p "$(CODENIB_SCIP_TOOLS_DIR)"; \
		curl -fL https://getcomposer.org/installer -o "$(CODENIB_SCIP_TOOLS_DIR)/composer-setup.php"; \
		php "$(CODENIB_SCIP_TOOLS_DIR)/composer-setup.php" \
			--install-dir="$(CODENIB_SCIP_TOOLS_DIR)" \
			--filename=composer; \
	fi
	@$(MAKE) --no-print-directory scip-php-info

scip-php-docker-tool:
	$(call require-command,docker)
	docker image inspect "$(COMPOSER_DOCKER_IMAGE)" >/dev/null 2>&1 || \
		docker pull "$(COMPOSER_DOCKER_IMAGE)"
	@$(MAKE) --no-print-directory scip-php-info

php-project-scip-tool:
	@test -n "$(PROJECT_ROOT)" || { echo "Set PROJECT_ROOT=/path/to/php/project" >&2; exit 1; }
	@$(CODENIB_TOOL_ENV) sh -eu -c 'project_root="$$(cd "$(PROJECT_ROOT)" && pwd)"; \
		if command -v php >/dev/null 2>&1 && command -v composer >/dev/null 2>&1; then \
			cd "$$project_root"; \
			composer require --dev "$(SCIP_PHP_PACKAGE)" --no-interaction --no-progress --no-security-blocking; \
			composer dump-autoload -o --no-interaction; \
			cd "$$project_root/vendor/davidrjenni/scip-php"; \
			composer install --no-interaction --no-progress --no-security-blocking; \
		else \
			command -v docker >/dev/null 2>&1 || { echo "Missing php/composer and docker. Run make scip-php-system-deps-ubuntu or make scip-php-docker-tool." >&2; exit 1; }; \
			docker run --rm --user "$$(id -u):$$(id -g)" -e HOME=/tmp -e COMPOSER_HOME=/tmp/composer -v "$$project_root:/app" -w /app "$(COMPOSER_DOCKER_IMAGE)" sh -lc \
				"git config --global --add safe.directory /app && composer require --dev $(SCIP_PHP_PACKAGE) --no-interaction --no-progress --no-security-blocking && composer dump-autoload -o --no-interaction"; \
			docker run --rm --user "$$(id -u):$$(id -g)" -e HOME=/tmp -e COMPOSER_HOME=/tmp/composer -v "$$project_root:/app" -w /app/vendor/davidrjenni/scip-php "$(COMPOSER_DOCKER_IMAGE)" \
				composer install --no-interaction --no-progress --no-security-blocking; \
		fi; \
		test -x "$$project_root/vendor/bin/scip-php"; \
		printf "PHP SCIP command ready: %s\n" "$$project_root/vendor/bin/scip-php"'

scip-php-system-deps-ubuntu:
	$(call require-command,sudo)
	$(call require-command,apt-get)
	sudo apt-get update
	sudo apt-get install -y $(SCIP_PHP_SYSTEM_PACKAGES)

lsp-smoke-tools: jdtls-tool csharp-lsp-tool ruby-lsp-tool intelephense-tool kotlin-lsp-tool
	@$(MAKE) --no-print-directory lsp-smoke-env

lsp-smoke-env:
	@echo "LSP smoke tools installed under: $(CODENIB_SCIP_TOOLS_DIR)"
	@echo "Use this environment for Java/C#/Ruby/PHP/Kotlin LSP smoke runs:"
	@echo "  export CODENIB_SCIP_TOOLS_DIR=$(CODENIB_SCIP_TOOLS_DIR)"
	@echo "  export DOTNET_ROOT=$(CODENIB_SCIP_TOOLS_DIR)/dotnet"
	@echo "  export GEM_HOME=$(CODENIB_SCIP_TOOLS_DIR)/gems"
	@echo "  export GEM_PATH=$(CODENIB_SCIP_TOOLS_DIR)/gems"
	@echo "  export GOBIN=$(CODENIB_SCIP_TOOLS_DIR)/go-tools/bin"
	@echo "  export GOPATH=$(CODENIB_SCIP_TOOLS_DIR)/go-tools"
	@echo "  export CODENIB_PHP_COMPOSER_IMAGE=$(COMPOSER_DOCKER_IMAGE)"
	@echo "  export PATH=$(CODENIB_TOOL_PATH):\$$PATH"

lsp-smoke: lsp-smoke-tools
	$(CODENIB_TOOL_ENV) python scripts/smoke_lsp_graph.py \
		--languages $(LSP_SMOKE_LANGUAGES) \
		--reference-languages $(LSP_SMOKE_REFERENCE_LANGUAGES) \
		$(foreach item,$(LSP_SMOKE_MIN_REFERENCES),--min-references $(item)) \
		--output-dir "$(LSP_SMOKE_OUTPUT_DIR)" \
		--json $(LSP_SMOKE_EXTRA_ARGS)

lsp-project-smoke: lsp-smoke-tools
	@test -n "$(PROJECT_LANGUAGE)" || { echo "Set PROJECT_LANGUAGE=<language>" >&2; exit 1; }
	@test -n "$(PROJECT_ROOT)" || { echo "Set PROJECT_ROOT=/path/to/project" >&2; exit 1; }
	$(CODENIB_TOOL_ENV) python scripts/smoke_lsp_graph.py \
		--languages "$(PROJECT_LANGUAGE)" \
		--project-root "$(PROJECT_ROOT)" \
		--output-dir "$(LSP_PROJECT_OUTPUT_DIR)" \
		--json $(LSP_PROJECT_EXTRA_ARGS)

graph-route-alignment-tools: $(GRAPH_ALIGNMENT_TOOL_TARGETS)
	@test -n "$(PROJECT_LANGUAGE)" || { echo "Set PROJECT_LANGUAGE=<language>" >&2; exit 1; }
	@echo "Graph route tools for $(PROJECT_LANGUAGE): $(GRAPH_ALIGNMENT_TOOL_TARGETS)"

graph-route-alignment: graph-route-alignment-tools
	@test -n "$(PROJECT_LANGUAGE)" || { echo "Set PROJECT_LANGUAGE=<language>" >&2; exit 1; }
	@test -n "$(PROJECT_ROOT)" || { echo "Set PROJECT_ROOT=/path/to/project" >&2; exit 1; }
	$(CODENIB_TOOL_ENV) python scripts/check_graph_route_alignment.py \
		--project-root "$(PROJECT_ROOT)" \
		--language "$(PROJECT_LANGUAGE)" \
		--reference-route "$(GRAPH_ALIGNMENT_REFERENCE_ROUTE)" \
		--candidate-route "$(GRAPH_ALIGNMENT_CANDIDATE_ROUTE)" \
		--output-dir "$(GRAPH_ALIGNMENT_OUTPUT_DIR)" \
		--skip-level "$(GRAPH_ALIGNMENT_SKIP_LEVEL)" \
		$(if $(GRAPH_ALIGNMENT_TARGET_DIR),--target-dir "$(GRAPH_ALIGNMENT_TARGET_DIR)",) \
		$(foreach pattern,$(GRAPH_ALIGNMENT_EXCLUDE_PATTERNS),--exclude-pattern "$(pattern)") \
		--json $(GRAPH_ALIGNMENT_EXTRA_ARGS)

large-scip-profile:
	$(CODENIB_TOOL_ENV) python scripts/profiling/profile_large_scip_repos.py \
		--manifest "$(LARGE_SCIP_PROFILE_MANIFEST)" \
		--cache-root "$(LARGE_SCIP_PROFILE_CACHE_ROOT)" \
		--output-dir "$(LARGE_SCIP_PROFILE_OUTPUT_DIR)" \
		--timeout "$(LARGE_SCIP_PROFILE_TIMEOUT)" \
		$(LARGE_SCIP_PROFILE_EXTRA_ARGS)

multilang-smoke: scip-cold-start-smoke lsp-smoke

multilang-registry-check:
	python -m pytest -q \
		test/test_languages.py \
		test/test_language_capability_matrix.py \
		test/test_ls_router_registry.py
	python scripts/language_capability_matrix.py --check docs/language_capabilities.md

lsp-smoke-system-deps-ubuntu:
	$(call require-command,sudo)
	$(call require-command,apt-get)
	sudo apt-get update
	sudo apt-get install -y $(SCIP_JDK_PACKAGE) $(SCIP_CANDIDATE_SYSTEM_PACKAGES) $(LSP_SMOKE_SYSTEM_PACKAGES)

jdtls-tool:
	$(call require-command,curl)
	$(call require-command,tar)
	@if ! command -v java >/dev/null 2>&1; then \
		echo "Missing java. On Ubuntu, run: make lsp-smoke-system-deps-ubuntu" >&2; \
		exit 1; \
	fi
	mkdir -p "$(CODENIB_SCIP_TOOLS_DIR)"
	@if [ ! -f "$(CODENIB_SCIP_TOOLS_DIR)/jdtls-$(JDTLS_VERSION)/.codenib-version" ] \
		|| ! grep -qx "$(JDTLS_VERSION)-$(JDTLS_BUILD)" "$(CODENIB_SCIP_TOOLS_DIR)/jdtls-$(JDTLS_VERSION)/.codenib-version"; then \
		rm -rf "$(CODENIB_SCIP_TOOLS_DIR)/jdtls-$(JDTLS_VERSION)" \
			"$(CODENIB_SCIP_TOOLS_DIR)/jdt-language-server-$(JDTLS_VERSION)-$(JDTLS_BUILD).tar.gz"; \
		curl -fL "$(JDTLS_URL)" \
			-o "$(CODENIB_SCIP_TOOLS_DIR)/jdt-language-server-$(JDTLS_VERSION)-$(JDTLS_BUILD).tar.gz"; \
		mkdir -p "$(CODENIB_SCIP_TOOLS_DIR)/jdtls-$(JDTLS_VERSION)"; \
		tar -xzf "$(CODENIB_SCIP_TOOLS_DIR)/jdt-language-server-$(JDTLS_VERSION)-$(JDTLS_BUILD).tar.gz" \
			-C "$(CODENIB_SCIP_TOOLS_DIR)/jdtls-$(JDTLS_VERSION)"; \
		echo "$(JDTLS_VERSION)-$(JDTLS_BUILD)" > "$(CODENIB_SCIP_TOOLS_DIR)/jdtls-$(JDTLS_VERSION)/.codenib-version"; \
	fi
	@{ \
		echo '#!/usr/bin/env sh'; \
		echo 'set -eu'; \
		echo 'base="$(CODENIB_SCIP_TOOLS_DIR)/jdtls-$(JDTLS_VERSION)"'; \
		echo 'launcher=$$(ls "$$base"/plugins/org.eclipse.equinox.launcher_*.jar | head -n 1)'; \
		echo 'workspace="$${JDTLS_WORKSPACE:-$${TMPDIR:-/tmp}/codenib-jdtls-workspace}"'; \
		echo 'mkdir -p "$$workspace"'; \
		echo 'exec java -Declipse.application=org.eclipse.jdt.ls.core.id1 -Dosgi.bundles.defaultStartLevel=4 -Declipse.product=org.eclipse.jdt.ls.core.product --add-modules=ALL-SYSTEM --add-opens java.base/java.util=ALL-UNNAMED --add-opens java.base/java.lang=ALL-UNNAMED -jar "$$launcher" -configuration "$$base/config_linux" -data "$$workspace" "$$@"'; \
	} > "$(CODENIB_SCIP_TOOLS_DIR)/jdtls"
	chmod +x "$(CODENIB_SCIP_TOOLS_DIR)/jdtls"

csharp-lsp-tool: scip-dotnet-tool

ruby-lsp-tool:
	@if ! command -v "$(RUBY_GEM)" >/dev/null 2>&1; then \
		echo "Missing $(RUBY_GEM). On Ubuntu, run: make scip-ruby-system-deps-ubuntu" >&2; \
		exit 1; \
	fi
	mkdir -p "$(CODENIB_SCIP_TOOLS_DIR)/gems"
	GEM_HOME="$(CODENIB_SCIP_TOOLS_DIR)/gems" \
	GEM_PATH="$(CODENIB_SCIP_TOOLS_DIR)/gems" \
		"$(RUBY_GEM)" install bundler -v "$(BUNDLER_VERSION)" --no-document
	GEM_HOME="$(CODENIB_SCIP_TOOLS_DIR)/gems" \
	GEM_PATH="$(CODENIB_SCIP_TOOLS_DIR)/gems" \
		"$(RUBY_GEM)" install ruby-lsp -v "$(RUBY_LSP_VERSION)" --no-document
	@rm -f "$(CODENIB_SCIP_TOOLS_DIR)/ruby-lsp"
	$(write-ruby-bundle-wrapper)
	$(write-ruby-lsp-wrapper)
	{ \
		printf '%s\n' '#!/usr/bin/env sh'; \
		printf '%s\n' 'set -eu'; \
		printf '%s\n' 'ruby_lsp_bin="$(CODENIB_SCIP_TOOLS_DIR)/ruby-lsp-bin"'; \
		printf '%s\n' 'if [ -f Gemfile ] && command -v bundle >/dev/null 2>&1; then'; \
		printf '%s\n' '  unset GEM_PATH'; \
		printf '%s\n' '  exec bundle exec "$$ruby_lsp_bin" "$$@"'; \
		printf '%s\n' 'fi'; \
		printf '%s\n' 'exec "$$ruby_lsp_bin" "$$@"'; \
	} > "$(CODENIB_SCIP_TOOLS_DIR)/ruby-lsp"; \
	chmod +x "$(CODENIB_SCIP_TOOLS_DIR)/ruby-lsp"

intelephense-tool:
	$(call require-command,npm)
	mkdir -p "$(CODENIB_SCIP_TOOLS_DIR)/node-tools"
	npm install --prefix "$(CODENIB_SCIP_TOOLS_DIR)/node-tools" \
		"intelephense@$(INTELEPHENSE_VERSION)"
	ln -sf "$(CODENIB_SCIP_TOOLS_DIR)/node-tools/node_modules/.bin/intelephense" \
		"$(CODENIB_SCIP_TOOLS_DIR)/intelephense"

kotlin-lsp-tool:
	$(call require-command,curl)
	$(call require-command,unzip)
	mkdir -p "$(CODENIB_SCIP_TOOLS_DIR)"
	@if [ ! -f "$(CODENIB_SCIP_TOOLS_DIR)/kotlin-lsp-$(KOTLIN_LSP_VERSION)/.codenib-version" ] \
		|| ! grep -qx "$(KOTLIN_LSP_VERSION)" "$(CODENIB_SCIP_TOOLS_DIR)/kotlin-lsp-$(KOTLIN_LSP_VERSION)/.codenib-version"; then \
		rm -rf "$(CODENIB_SCIP_TOOLS_DIR)/kotlin-lsp-$(KOTLIN_LSP_VERSION)" \
			"$(CODENIB_SCIP_TOOLS_DIR)/kotlin-lsp-$(KOTLIN_LSP_VERSION).vsix"; \
		curl -fL "$(KOTLIN_LSP_URL)" \
			-o "$(CODENIB_SCIP_TOOLS_DIR)/kotlin-lsp-$(KOTLIN_LSP_VERSION).vsix"; \
		mkdir -p "$(CODENIB_SCIP_TOOLS_DIR)/kotlin-lsp-$(KOTLIN_LSP_VERSION)"; \
		unzip -q -o "$(CODENIB_SCIP_TOOLS_DIR)/kotlin-lsp-$(KOTLIN_LSP_VERSION).vsix" \
			-d "$(CODENIB_SCIP_TOOLS_DIR)/kotlin-lsp-$(KOTLIN_LSP_VERSION)"; \
		echo "$(KOTLIN_LSP_VERSION)" > "$(CODENIB_SCIP_TOOLS_DIR)/kotlin-lsp-$(KOTLIN_LSP_VERSION)/.codenib-version"; \
	fi
	@{ \
		echo '#!/usr/bin/env sh'; \
		echo 'set -eu'; \
		echo 'base="$(CODENIB_SCIP_TOOLS_DIR)/kotlin-lsp-$(KOTLIN_LSP_VERSION)"'; \
		echo 'server=$$(find "$$base" -type f \( -name intellij-server -o -name kotlin-lsp.sh \) | head -n 1)'; \
		echo 'if [ -z "$$server" ]; then echo "No Kotlin LSP launcher found under $$base" >&2; exit 1; fi'; \
		echo 'chmod +x "$$server"'; \
		echo 'exec "$$server" "$$@"'; \
	} > "$(CODENIB_SCIP_TOOLS_DIR)/kotlin-language-server"
	chmod +x "$(CODENIB_SCIP_TOOLS_DIR)/kotlin-language-server"

dev:
	pip install -e ".[dev,test]"

test:
	pytest -m "not slow and not integration and not integration_serial and not integration_serial_consumer" -x --tb=short

test-all:
	pytest

test-slow:
	pytest -m "slow" --tb=short

ASK_REPO ?= .
ASK_QUALITY_ARGS ?=

ask-quality:
	python scripts/check_ask_retrieval.py --repo "$(ASK_REPO)" $(ASK_QUALITY_ARGS)

branding-assets:
	python scripts/check_namespace.py --sync-assets

branding-check:
	python scripts/check_namespace.py

# Rerank latency benchmark — measures wall-clock + VRAM across rerank backends.
# Candidates: 25 50 100  |  Reps: 3  |  Output: scripts/bench_rerank_results.md
#
#   make bench-rerank                              # embedding_cached, st_cross, qwen_cross
#   make bench-rerank BENCH_BACKENDS="st_cross qwen_cross"
#   make bench-rerank BENCH_N="10 25 50"
#   make bench-rerank BENCH_INCLUDE_LLM=--include-llm  # also runs LLM listwise (slow)
BENCH_BACKENDS ?= embedding embedding_cached st_cross qwen_cross
BENCH_N        ?= 25 50 100
BENCH_REPS     ?= 3
BENCH_INCLUDE_LLM ?=

bench-rerank:
	python scripts/bench_rerank_latency.py \
		--backends $(BENCH_BACKENDS) \
		--candidates $(BENCH_N) \
		--reps $(BENCH_REPS) \
		$(BENCH_INCLUDE_LLM)

web-deps:
	$(call require-command,npm)
	npm install --prefix web

web-start:
	./scripts/dev_web.sh start

web-stop:
	./scripts/dev_web.sh stop

web-restart:
	./scripts/dev_web.sh restart

web-reclaim:
	CODENIB_WEB_RECLAIM_PORTS=1 ./scripts/dev_web.sh restart

web-status:
	./scripts/dev_web.sh status

web-logs:
	./scripts/dev_web.sh logs

web-follow:
	./scripts/dev_web.sh follow
