#!/usr/bin/env bash
# ==============================================================================
# ATEZ Mevzuat Radarı - macOS ARM64 Self-Hosted Runner Kurulum Betiği
# ==============================================================================

set -e

RUNNER_DIR="$HOME/actions-runner-atez"
REPO_URL="https://github.com/alicankocak/atez-mevzuat-source-archiver-gemini"
LATEST_RUNNER_VER="2.321.0"
RUNNER_ARCH="osx-arm64"

echo "=== ATEZ Self-Hosted Runner Kurulumu Başlatılıyor ==="
echo "Dizin: $RUNNER_DIR"
echo "Hedef Depo: $REPO_URL"

mkdir -p "$RUNNER_DIR"
cd "$RUNNER_DIR"

if [ ! -f "config.sh" ]; then
    echo "GitHub Actions Runner indiriliyor (v${LATEST_RUNNER_VER} - ${RUNNER_ARCH})..."
    curl -o "actions-runner-${RUNNER_ARCH}-${LATEST_RUNNER_VER}.tar.gz" -L "https://github.com/actions/runner/releases/download/v${LATEST_RUNNER_VER}/actions-runner-${RUNNER_ARCH}-${LATEST_RUNNER_VER}.tar.gz"
    tar xzf "./actions-runner-${RUNNER_ARCH}-${LATEST_RUNNER_VER}.tar.gz"
    rm -f "./actions-runner-${RUNNER_ARCH}-${LATEST_RUNNER_VER}.tar.gz"
fi

echo ""
echo "=========================================================================="
echo "Runner kurulum dosyaları hazır!"
echo "Şimdi GitHub deponuzdan Token almanız gerekmektedir:"
echo "1. GitHub'da $REPO_URL/settings/actions/runners/new adresine gidin."
echo "2. Size verilen token ile şu komutu çalıştırın:"
echo ""
echo "   cd $RUNNER_DIR"
echo "   ./config.sh --url $REPO_URL --token <TOKEN_DEGERI> --labels macOS,arm64"
echo ""
echo "3. Runner'ı arka planda bir macOS servisi (launchd) olarak başlatmak için:"
echo "   ./svc.sh install"
echo "   ./svc.sh start"
echo "=========================================================================="
