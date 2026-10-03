"""Build the platform-specific GUI, test its bundled runtime, and package it."""
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import urllib.request
import zipfile

ROOT=Path(__file__).resolve().parents[1]
GUI=ROOT/'gui'
NAME='FF2-Korean-Patcher'
PATCH='ff2-ko-full-reviewed-v094.ff2patch.gz'
DIGEST='a76367bab11d341f126fb75069e890fd407922925a45fc26233147400f54634a'
URL='https://github.com/Jungsik-won/Fantastic-Fortune-2-Triple-Star-Korean-Translation/releases/download/v0.9.4/'+PATCH
README='''판타스틱 포츈 2: 트리플 스타 한글 검수판 v0.9.4 / GUI 1.4

1. 꾸러미를 압축 해제하고 FF2-Korean-Patcher 앱 또는 실행 파일을 엽니다.
2. ‘원본 ISO 선택 → 자동 패치’를 누르고 일본판 SLPS-25396 원본 ISO를 고릅니다.
3. 원본 확인·패치·완성 ISO 검증이 자동으로 진행됩니다.
4. ‘완성 파일 폴더 열기’를 눌러 새 ISO를 PCSX2에서 새로 부팅합니다.

Python 설치와 명령어 입력, 별도의 패치 파일 선택이 필요하지 않습니다.
패치는 앱에 포함돼 있으며 모든 ISO 작업은 로컬에서 진행됩니다.
원본과 같은 폴더에 ‘원본 이름 (Korean v0.9.4).iso’를 저장합니다.
같은 이름이 있으면 번호를 붙여 새 파일로 만듭니다. 원본은 덮어쓰지 않습니다.
기본 폴더에 쓸 수 없으면 ISO 선택 전에 ‘저장 폴더 변경’을 사용하세요.
일반적으로 3.1 GiB 이상의 여유 공간이 필요합니다. 하드 링크를 지원하지 않는
USB/외장 저장소에서는 마지막 저장에 추가 복사 공간이 필요할 수 있으므로
6.1 GiB 이상을 확보하거나 컴퓨터 내장 디스크에 저장해 주세요.
이전 버전 에뮬레이터 상태 저장을 불러오지 말고 새로 부팅해 주세요.

원본 ISO 크기: 3,231,907,840바이트
원본 SHA-256: a92f19c3402592aaabd0f1c4fdd67a839c32cde5cd133e861c16bacca8322ac7
완성 ISO SHA-256: 241989cc367a9369f872c13be187d6cf4120736a7ceb03004258ab0908c170cb

전체 대사·선택지의 1차 문맥 검수는 반영했습니다.
모든 루트·엔딩의 실제 플레이 검증은 아직 진행 중입니다.
Mac 앱은 개발자 공증을 받지 않은 앱입니다. macOS가 실행을 차단하면
시스템 설정 → 개인정보 보호 및 보안에서 앱 이름과 출처를 확인한 뒤
‘그래도 열기’를 사용할 수 있습니다.
원본 ISO와 BIOS는 포함하지 않습니다.
https://github.com/Jungsik-won/Fantastic-Fortune-2-Triple-Star-Korean-Translation
'''

def main():
    resources=ROOT/'build-resources';resources.mkdir(exist_ok=True)
    patch=resources/PATCH
    if not patch.exists():
        with urllib.request.urlopen(URL,timeout=90) as response:patch.write_bytes(response.read())
    assert hashlib.sha256(patch.read_bytes()).hexdigest()==DIGEST
    cmd=[sys.executable,'-m','PyInstaller','--noconfirm','--clean','--windowed',
         '--name',NAME,'--add-data',str(patch)+os.pathsep+'.','--paths',str(GUI)]
    if sys.platform=='win32':cmd.append('--onefile')
    else:cmd.extend(['--onedir','--osx-bundle-identifier','org.jswon.ff2-korean-patcher'])
    cmd.append(str(GUI/'ff2_gui.py'))
    subprocess.run(cmd,cwd=ROOT,check=True)
    dist=ROOT/'dist'
    if sys.platform=='win32':binary=dist/(NAME+'.exe')
    else:binary=dist/(NAME+'.app')/'Contents/MacOS'/NAME
    report=ROOT/'bundled-smoke.json'
    subprocess.run([str(binary),'--smoke-test',str(report)],timeout=90,check=True)
    result=json.loads(report.read_text())
    assert result['ok'] and result['bundled_patch_spans']==100531
    label='windows-x64' if sys.platform=='win32' else ('macos-apple-silicon' if platform.machine()=='arm64' else 'macos-intel')
    out=ROOT/'release-assets';out.mkdir(exist_ok=True)
    stage=ROOT/'FF2-Korean-Patcher-GUI';stage.mkdir(exist_ok=True)
    (stage/'사용 안내.txt').write_text(README,encoding='utf-8')
    (stage/'실행 확인.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    # Include notices from the bundled runtime and third-party packages.
    notices=stage/'third-party-notices';notices.mkdir(exist_ok=True)
    import importlib.metadata as metadata
    for pkg in ('pyinstaller','pyinstaller-hooks-contrib','altgraph','packaging'):
        d=metadata.distribution(pkg)
        for item in d.files or []:
            if any(token in str(item).lower() for token in ('license','copying','copyright')):
                source=d.locate_file(item)
                if source.is_file():shutil.copyfile(source,notices/(pkg+'-'+Path(item).name))
    # CPython's distribution license (contains Tcl/Tk and bundled library notices).
    license_path=Path(sys.base_prefix)/'LICENSE.txt'
    if not license_path.exists():license_path=Path(sys.base_prefix)/'LICENSE'
    if license_path.exists():shutil.copyfile(license_path,notices/'Python-LICENSE.txt')
    archive=out/('FF2-Korean-Patcher-GUI-1.4-'+label+'.zip')
    if sys.platform=='win32':
        shutil.copy2(binary,stage/binary.name)
        with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
            for p in stage.rglob('*'):
                if p.is_file():z.write(p,str(p.relative_to(stage)))
    else:
        app=dist/(NAME+'.app')
        subprocess.run(['ditto',str(app),str(stage/app.name)],check=True)
        subprocess.run(['ditto','-c','-k','--sequesterRsrc','--keepParent',str(stage),str(archive)],check=True)
    print(archive)
    print(hashlib.sha256(archive.read_bytes()).hexdigest())

if __name__=='__main__':main()
