'use strict';

window.createSpikeMarkStage = function (host, actions) {
  const T = window.THREE;
  if (!T) throw new Error('未加载本地 Three.js');
  const renderer = new T.WebGLRenderer({ antialias: true });
  const canvas = renderer.domElement;
  canvas.setAttribute('aria-label', '三维摆位视窗');
  canvas.tabIndex = -1;
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  const scene = new T.Scene();
  scene.background = new T.Color(0x1a1d20);
  scene.add(new T.HemisphereLight(0xf5f1e9, 0x717781, 1.6));
  const light = new T.DirectionalLight(0xfff3df, 2);
  light.position.set(3, 7, 4);
  scene.add(light);

  const camera = new T.PerspectiveCamera(48, 1, .02, 300);
  const orbit = new T.Spherical(9, 1, .65);
  const target = new T.Vector3(0, .7, 0);
  const up = new T.Vector3(0, 1, 0);
  const ray = new T.Raycaster();
  ray.params.Line.threshold = .04;
  const floorPlane = new T.Plane(up, 0);
  const models = new T.Group();
  const room = new T.Group();
  const marker = new T.Group();
  const arrow = new T.ArrowHelper(up, new T.Vector3(), .9, 0xeab774, .24, .25);
  const cameraBody = new T.Mesh(new T.BoxGeometry(.24, .18, .28), new T.MeshBasicMaterial({ color: 0xeab774 }));
  marker.add(arrow, cameraBody);
  marker.name = '相机';
  marker.visible = false;
  const landing = new T.Mesh(new T.RingGeometry(.09, .15, 24),
    new T.MeshBasicMaterial({ color: 0xa5e6c7, depthWrite: false }));
  landing.rotation.x = -Math.PI / 2;
  landing.visible = false;
  scene.add(room, models, marker, landing);
  const nodes = new Map();
  const materials = {
    human: new T.MeshStandardMaterial({ color: 0x9e978a, roughness: .88 }),
    joint: new T.MeshStandardMaterial({ color: 0x746e65, roughness: .9 }),
    box: new T.MeshStandardMaterial({ color: 0x7c8995, roughness: .88 }),
    prop: new T.MeshStandardMaterial({ color: 0x99836e, roughness: .9 }),
    selected: new T.LineBasicMaterial({ color: 0xd4ab79 }),
    lift: new T.MeshBasicMaterial({ color: 0xa1babe })
  };
  let documentModel, ground, gesture, assetKind, frame = 0, failed = false;
  let maxDistance = 100;
  const eye = new T.Vector3();
  const firstAngles = new T.Euler(0, 0, 0, 'YXZ');
  const walking = new Set();
  const precisionKeys = new Set();
  const movementKeys = ['KeyW', 'KeyA', 'KeyS', 'KeyD'];
  const walkSpeed = 1.6, precisionSpeed = .45, mouseSensitivity = .0012;
  const firstEvents = new AbortController();
  const listener = { signal: firstEvents.signal };
  let firstPending, firstActive = false, firstAsset, grabbed, lastPose, walkTime, reportedPose;

  function cancelOrbitGesture() {
    const pointer = gesture?.pointer;
    gesture = null;
    if (pointer !== undefined && canvas.hasPointerCapture(pointer)) canvas.releasePointerCapture(pointer);
    assetKind = null;
    host.removeAttribute('data-drop');
  }

  function cancel() {
    cancelOrbitGesture();
    exitFirstPerson();
  }

  function fail(error) {
    if (failed) return;
    failed = true;
    cancel();
    firstEvents.abort();
    cancelAnimationFrame(frame);
    observer.disconnect();
    canvas.remove();
    renderer.dispose();
    actions.unavailable(error);
  }

  function redraw() {
    if (frame || failed) return;
    frame = requestAnimationFrame(time => {
      frame = 0;
      try {
        stepFirstPerson(time);
        const { clientWidth: width, clientHeight: height } = host;
        if (!width || !height) return;
        if (camera.aspect !== width / height || canvas.width !== Math.floor(width * renderer.getPixelRatio()) ||
            canvas.height !== Math.floor(height * renderer.getPixelRatio())) {
          camera.aspect = width / height;
          camera.updateProjectionMatrix();
          renderer.setSize(width, height, false);
        }
        view();
        if (firstActive && actions.firstPersonPose) {
          const pose = firstPose();
          if (!reportedPose || pose.position.some((value, i) => value !== reportedPose.position[i]) ||
              pose.target.some((value, i) => value !== reportedPose.target[i])) {
            reportedPose = pose;
            actions.firstPersonPose(pose);
          }
        }
        updateLanding();
        renderer.render(scene, camera);
        if (firstActive && walking.size) redraw();
      } catch (error) { fail(error); }
    });
  }

  function view() {
    if (firstActive) {
      camera.position.copy(eye);
      camera.rotation.copy(firstAngles);
    } else {
      camera.position.setFromSpherical(orbit).add(target);
      camera.lookAt(target);
    }
    camera.updateMatrixWorld();
  }

  function fit() {
    if (!documentModel || firstActive || firstPending) return;
    const bounds = new T.Box3().setFromObject(models);
    bounds.expandByPoint(marker.position);
    const [width, depth] = ground;
    bounds.expandByPoint(new T.Vector3(-width / 2, 0, -depth / 2));
    bounds.expandByPoint(new T.Vector3(width / 2, 0, depth / 2));
    const size = bounds.getSize(new T.Vector3());
    bounds.getCenter(target);
    target.y = Math.max(.6, target.y);
    const extent = Math.max(3, size.length());
    const aspect = Math.max(.1, host.clientWidth / Math.max(1, host.clientHeight));
    orbit.set(extent * 1.3 / Math.min(1, aspect), 1, .65);
    maxDistance = Math.max(100, orbit.radius * 3, ...ground);
    camera.far = maxDistance * 5;
    camera.updateProjectionMatrix();
    redraw();
  }

  function clear(group, sharedMaterials = false) {
    group.traverse(object => {
      object.geometry?.dispose();
      if (!sharedMaterials) object.material?.dispose();
    });
    group.clear();
  }

  function buildRoom() {
    clear(room);
    const [width, depth] = ground;
    const height = 3;
    const floor = new T.Mesh(new T.PlaneGeometry(width, depth), new T.MeshBasicMaterial({ color: 0x303338 }));
    floor.rotation.x = -Math.PI / 2;
    room.add(floor);
    const divisions = Math.max(2, Math.min(40, Math.ceil(Math.max(width, depth))));
    const grid = new T.GridHelper(1, divisions, 0x595d63, 0x41454a);
    grid.scale.set(width, 1, depth);
    grid.position.y = .003;
    room.add(grid);
    const box = new T.BoxGeometry(width, height, depth);
    const outline = new T.LineSegments(new T.EdgesGeometry(box), new T.LineBasicMaterial({ color: 0x617479 }));
    box.dispose();
    outline.position.y = height / 2;
    room.add(outline);
  }

  function setGround(size) {
    if (failed || (ground && size.every((value, index) => value === ground[index]))) return;
    ground = size;
    if (firstActive) clampEye();
    buildRoom();
    maxDistance = Math.max(100, orbit.radius * 3, ...ground);
    camera.far = maxDistance * 5;
    camera.updateProjectionMatrix();
    redraw();
  }

  function mannequinPart(a, b, radius, index) {
    const start = new T.Vector3().fromArray(a);
    const direction = new T.Vector3().fromArray(b).sub(start);
    const half = direction.length() / 2;
    const r = radius;
    const profile = index === 0 ? [[0, -half-r*.55], [.48*r, -half-r*.44], [.72*r, -half],
      [.78*r, -half*.35], [.98*r, half*.75], [.88*r, half+r*.20], [.50*r, half+r*.50], [0, half+r*.53]] :
      index === 2 ? [[0, -half-r*.72], [.52*r, -half-r*.63], [.84*r, -half], [.93*r, half+r*.17],
        [.68*r, half+r*.72], [.32*r, half+r*.92], [0, half+r]] :
      [[0, -half-r*.65], [.45*r, -half-r*.56], [.86*r, -half], [.70*r, half],
        [.43*r, half+r*.57], [0, half+r*.68]];
    const foot = index === 7 || index === 12;
    const geometry = foot ? block([r*1.65, r*1.7, half*2+r*1.7]) :
      new T.LatheGeometry(profile.map(([x, y]) => new T.Vector2(x, y)), index === 2 ? 14 : 10);
    if (!foot) geometry.scale(index === 0 ? 1 : .93, 1, index === 0 ? .72 : .91);
    const mesh = new T.Mesh(geometry, index === 1 ? materials.joint : materials.human);
    poseMannequinPart(mesh, start, direction, radius, index);
    return mesh;
  }

  function poseMannequinPart(mesh, start, direction, radius, index) {
    mesh.position.copy(start).addScaledVector(direction, .5);
    if (index === 7 || index === 12) mesh.position.y -= radius * .15;
    else if (direction.lengthSq()) mesh.quaternion.setFromUnitVectors(up, direction.normalize());
    else mesh.quaternion.identity();
  }

  function block([x, y, z]) {
    const bevel = Math.min(.022, x*.17, y*.17, z*.17);
    const w = x/2-bevel, h = y/2-bevel;
    const shape = new T.Shape();
    shape.moveTo(-w, -h);
    shape.lineTo(w, -h);
    shape.lineTo(w, h);
    shape.lineTo(-w, h);
    shape.closePath();
    return new T.ExtrudeGeometry(shape, { depth: z-2*bevel, bevelEnabled: true,
      bevelThickness: bevel, bevelSize: bevel, bevelSegments: 1, steps: 1 }).translate(0, 0, bevel-z/2);
  }

  function load(doc, humanParts, propParts, reset) {
    try {
      if (documentModel && (reset || doc.shot.id !== documentModel.shot.id || doc.scene.id !== documentModel.scene.id)) {
        cancel();
        lastPose = null;
      }
      documentModel = doc;
      setGround(doc.scene.ground.size);
      clear(models, true);
      nodes.clear();
      for (const entity of doc.scene.entities) {
        const node = new T.Group();
        const parts = Object.hasOwn(propParts, entity.id) ? propParts[entity.id] : null;
        const material = parts ? materials.prop : (materials[entity.kind] ?? materials.prop);
        if (entity.kind === 'human') {
          const parts = Object.hasOwn(humanParts, entity.id) ? humanParts[entity.id] : null;
          if (!Array.isArray(parts) || !parts.length) throw new Error('场景未返回人物几何');
          node.userData.humanParts = parts;
          node.userData.humanMeshes = parts.map(([a, b, radius], index) => mannequinPart(a, b, radius, index));
          node.add(...node.userData.humanMeshes);
        } else if (parts) {
          for (const [center, size] of parts) {
            const mesh = new T.Mesh(block(size), material);
            mesh.position.fromArray(center);
            node.add(mesh);
          }
        } else {
          const box = new T.Mesh(block(entity.size), material);
          box.position.y = entity.size[1] / 2;
          node.add(box);
        }
        if (typeof actions.lift === 'function') {
          const top = new T.Box3().setFromObject(node).max.y;
          const stem = new T.Mesh(new T.CylinderGeometry(.035, .035, .18, 8), materials.lift);
          stem.position.y = top + .17;
          const tip = new T.Mesh(new T.ConeGeometry(.1, .16, 12), materials.lift);
          tip.position.y = top + .34;
          stem.userData.lift = tip.userData.lift = true;
          node.userData.liftMeshes = [stem, tip];
          node.add(stem, tip);
        }
        const halfWidth = entity.kind === 'human' ? entity.body.shoulder/2+.16 :
          (entity.size?.[0] ?? Math.max(...parts.map(([center, size]) => Math.abs(center[0])*2 + size[0])))/2+.06;
        const halfDepth = entity.kind === 'human' ? .23 :
          (entity.size?.[2] ?? Math.max(...parts.map(([center, size]) => Math.abs(center[2])*2 + size[2])))/2+.06;
        const corners = [[-1,-1], [1,-1], [1,1], [-1,1]].flatMap(([x, z]) => [
          new T.Vector3(x*halfWidth*.72, .012, z*halfDepth),
          new T.Vector3(x*halfWidth, .012, z*halfDepth),
          new T.Vector3(x*halfWidth, .012, z*halfDepth),
          new T.Vector3(x*halfWidth, .012, z*halfDepth*.72)
        ]);
        const selection = new T.LineSegments(new T.BufferGeometry().setFromPoints(corners), materials.selected);
        selection.raycast = () => {};
        node.add(selection);
        node.userData.selection = selection;
        node.userData.entity = entity;
        node.name = entity.name || entity.id;
        models.add(node);
        nodes.set(entity.id, node);
      }
      if (grabbed && !nodes.has(grabbed)) grabbed = null;
      marker.visible = true;
      updateDrafts(new Map(), '');
      if (reset) fit();
      return !failed;
    } catch (error) {
      fail(error);
      return false;
    }
  }

  function playPose({ transforms, instances }) {
    if (failed || !documentModel) return false;
    if (firstActive || firstPending) cancel();
    const { entities } = documentModel.scene;
    // 帧页以当前加载的已提交场景为基线；人体段长和半径保持不变。
    const nodeAt = index => nodes.get(entities[index]?.id);
    for (const [index, transform] of transforms) {
      const node = nodeAt(index);
      node.userData.playTransform = transform;
      node.position.fromArray(transform.pos);
      node.position.y = Math.max(0, node.position.y);
      node.rotation.y = T.MathUtils.degToRad(transform.yaw ?? 0);
    }
    const samePoint = (a, b) => a.every((value, index) => value === b[index]);
    const start = new T.Vector3(), direction = new T.Vector3(), bounds = new T.Box3();
    for (const [index, parts] of instances) {
      const data = nodeAt(index).userData;
      let changed = false;
      parts.forEach(([a, b, radius], i) => {
        if (samePoint(a, data.humanParts[i][0]) && samePoint(b, data.humanParts[i][1])) return;
        changed = true;
        start.fromArray(a);
        direction.fromArray(b).sub(start);
        poseMannequinPart(data.humanMeshes[i], start, direction, radius, i);
      });
      data.humanParts = parts;
      if (changed && data.liftMeshes) {
        let top = -Infinity;
        for (const mesh of data.humanMeshes) {
          mesh.updateMatrix();
          top = Math.max(top, bounds.copy(mesh.geometry.boundingBox).applyMatrix4(mesh.matrix).max.y);
        }
        data.liftMeshes[0].position.y = top + .17;
        data.liftMeshes[1].position.y = top + .34;
      }
    }
    redraw();
    return true;
  }

  function updateDrafts(drafts, selected, cameraDraft) {
    if (failed || !documentModel) return;
    for (const [id, node] of nodes) {
      const transform = node.userData.playTransform ?? node.userData.entity.transform;
      const draft = drafts.get(id);
      const value = (key, fallback) => {
        const input = draft?.[key];
        return (typeof input === 'number' || (typeof input === 'string' && input.trim())) &&
          Number.isFinite(Number(input)) ? Number(input) : fallback;
      };
      node.position.set(value('x', transform.pos[0]), Math.max(0, value('y', transform.pos[1])), value('z', transform.pos[2]));
      node.rotation.y = T.MathUtils.degToRad(value('yaw', transform.yaw ?? 0));
      node.userData.selection.visible = id === selected;
    }
    const validPoint = point => Array.isArray(point) && point.length === 3 &&
      Number.isFinite(point[0]) && Number.isFinite(point[1]) && Number.isFinite(point[2]);
    const { pos, look_at } = validPoint(cameraDraft?.pos) && validPoint(cameraDraft?.look_at) ?
      cameraDraft : documentModel.camera;
    marker.position.fromArray(pos);
    const forward = new T.Vector3().fromArray(look_at).sub(marker.position);
    arrow.visible = forward.lengthSq() > 0;
    if (arrow.visible) {
      forward.normalize();
      arrow.setDirection(forward);
      cameraBody.quaternion.setFromUnitVectors(new T.Vector3(0, 0, 1), forward);
    } else {
      cameraBody.quaternion.identity();
    }
    redraw();
  }

  function firstStatus(text, isError = false) {
    actions.firstPersonStatus?.(text, isError);
  }

  function clampEye() {
    const [width, depth] = ground;
    eye.x = T.MathUtils.clamp(eye.x, -width / 2 + .05, width / 2 - .05);
    eye.z = T.MathUtils.clamp(eye.z, -depth / 2 + .05, depth / 2 - .05);
  }

  function stepFirstPerson(time) {
    if (!firstActive || !walking.size) return;
    const dt = walkTime === undefined ? 0 : Math.min((time - walkTime) / 1000, .05);
    walkTime = time;
    const x = Number(walking.has('KeyD')) - Number(walking.has('KeyA'));
    const z = Number(walking.has('KeyS')) - Number(walking.has('KeyW'));
    if (!x && !z) return;
    const speed = precisionKeys.size ? precisionSpeed : walkSpeed;
    const distance = speed * dt / Math.hypot(x, z);
    const sin = Math.sin(firstAngles.y), cos = Math.cos(firstAngles.y);
    eye.x += distance * (cos * x + sin * z);
    eye.z += distance * (cos * z - sin * x);
    clampEye();
  }

  function firstPose() {
    const forward = new T.Vector3(0, 0, -1).applyEuler(firstAngles);
    return { position: eye.toArray(), target: forward.add(eye).toArray() };
  }

  function useFirstPersonCamera() {
    const pose = firstActive ? firstPose() : lastPose;
    if (!pose) {
      firstStatus('请先进入第一人称，再将视角写为机位。', true);
      return false;
    }
    if (!actions.editable() || typeof actions.firstPersonCamera !== 'function') {
      firstStatus('当前不可编辑拍摄机位。', true);
      return false;
    }
    actions.firstPersonCamera(pose.position.slice(), pose.target.slice());
    return true;
  }

  function setFirstPersonAsset(kind) {
    firstAsset = kind || null;
    if (firstActive) redraw();
  }

  function enterFirstPerson() {
    if (firstActive && document.pointerLockElement === canvas) return true;
    if (firstActive) exitFirstPerson();
    if (firstPending && !firstPending.canceled) return true;
    if (failed || !documentModel || !actions.editable()) {
      firstStatus('当前不可进入第一人称。', true);
      return false;
    }
    if (firstPending) return false;
    if (typeof canvas.requestPointerLock !== 'function') {
      firstStatus('此环境不支持鼠标锁定，仍可使用环绕视角。', true);
      return false;
    }
    cancelOrbitGesture();
    const pending = firstPending = {};
    const denied = () => {
      if (firstPending !== pending) return;
      clearTimeout(pending.timer);
      firstPending = null;
      if (!pending.canceled) firstStatus('未能锁定鼠标，未进入第一人称。请保持窗口在前台并重试。', true);
    };
    pending.timer = setTimeout(denied, 2000);
    try {
      // 在同一次用户手势中，先让三维画布获得焦点，再请求锁定。
      canvas.focus({ preventScroll: true });
      canvas.requestPointerLock()?.catch(denied);
      return true;
    } catch {
      denied();
      return false;
    }
  }

  function exitFirstPerson() {
    if (firstPending) firstPending.canceled = true;
    if (firstActive) {
      lastPose = firstPose();
      firstActive = false;
      reportedPose = undefined;
      walking.clear();
      precisionKeys.clear();
      walkTime = undefined;
      grabbed = null;
      landing.visible = false;
      actions.firstPersonMode?.(false);
      redraw();
    }
    if (document.pointerLockElement === canvas) document.exitPointerLock();
  }

  document.addEventListener('pointerlockchange', () => {
    if (document.pointerLockElement !== canvas) {
      if (firstActive) {
        exitFirstPerson();
        firstStatus('鼠标已解锁，已返回环绕视角。');
      }
      return;
    }
    if (firstActive) return;
    const pending = firstPending;
    clearTimeout(pending?.timer);
    firstPending = null;
    if (!pending || pending.canceled || !actions.editable() || document.hidden) {
      document.exitPointerLock();
      return;
    }
    if (lastPose) {
      eye.fromArray(lastPose.position);
      const direction = new T.Vector3().fromArray(lastPose.target).sub(eye).normalize();
      firstAngles.set(Math.asin(direction.y), Math.atan2(-direction.x, -direction.z), 0);
    } else {
      view();
      eye.copy(camera.position).lerp(target, .65);
      firstAngles.set(-.18, orbit.theta, 0);
    }
    eye.y = 1.65;
    clampEye();
    firstActive = true;
    canvas.title = '';
    actions.firstPersonMode?.(true);
    redraw();
  }, listener);
  document.addEventListener('pointerlockerror', () => {
    if (!firstPending) return;
    const { canceled, timer } = firstPending;
    clearTimeout(timer);
    firstPending = null;
    if (!canceled) firstStatus('未能锁定鼠标，未进入第一人称。请保持窗口在前台并重试。', true);
  }, listener);
  window.addEventListener('blur', cancel, listener);
  document.addEventListener('visibilitychange', () => { if (document.hidden) cancel(); }, listener);
  document.addEventListener('mousemove', event => {
    if (!firstActive) return;
    const sensitivity = mouseSensitivity * (precisionKeys.size ? .5 : 1);
    firstAngles.y -= event.movementX * sensitivity;
    firstAngles.x = T.MathUtils.clamp(firstAngles.x - event.movementY * sensitivity, -1.55, 1.55);
    redraw();
  }, listener);
  window.addEventListener('keydown', event => {
    if (event.code === 'Escape' && (firstActive || firstPending)) {
      exitFirstPerson();
      return;
    }
    if (!firstActive || event.ctrlKey || event.altKey || event.metaKey) return;
    if (event.code === 'ShiftLeft' || event.code === 'ShiftRight') {
      precisionKeys.add(event.code);
    } else if (movementKeys.includes(event.code)) {
      event.preventDefault();
      if (!walking.size) walkTime = undefined;
      walking.add(event.code);
      redraw();
    } else if (event.code === 'KeyR' || event.code === 'KeyE') {
      event.preventDefault();
      if (event.repeat) return;
      if (event.code === 'KeyE') useFirstPersonCamera();
      else if (grabbed) {
        grabbed = null;
        firstStatus('已取消抓取，摆位未改动。');
        redraw();
      }
    }
  }, { ...listener, capture: true });
  window.addEventListener('keyup', event => {
    precisionKeys.delete(event.code);
    walking.delete(event.code);
  }, { ...listener, capture: true });

  function aim(event) {
    view();
    if (firstActive) {
      ray.setFromCamera({ x: 0, y: 0 }, camera);
    } else {
      const rect = canvas.getBoundingClientRect();
      ray.setFromCamera({ x: 2 * (event.clientX - rect.left) / rect.width - 1,
        y: 1 - 2 * (event.clientY - rect.top) / rect.height }, camera);
    }
  }

  function floorPoint(held = null) {
    aim();
    const point = ray.ray.intersectPlane(floorPlane, new T.Vector3());
    const [width, depth] = ground;
    if (!point || Math.abs(point.x) > width / 2 || Math.abs(point.z) > depth / 2) return null;
    const node = held === null ? null : nodes.get(held);
    const parts = node?.userData.humanParts;
    if (!parts) return point;
    // 预览使用当前显示的胶囊和朝向；确认仍走编辑器现有的边界校验。
    const cos = Math.cos(node.rotation.y), sin = Math.sin(node.rotation.y);
    let minX = -Infinity, maxX = Infinity, minZ = -Infinity, maxZ = Infinity;
    for (const [a, b, radius] of parts) {
      for (const [x, , z] of [a, b]) {
        const rx = cos * x + sin * z, rz = -sin * x + cos * z;
        minX = Math.max(minX, -width / 2 - rx + radius);
        maxX = Math.min(maxX, width / 2 - rx - radius);
        minZ = Math.max(minZ, -depth / 2 - rz + radius);
        maxZ = Math.min(maxZ, depth / 2 - rz - radius);
      }
    }
    if (minX > maxX || minZ > maxZ) return null;
    point.x = T.MathUtils.clamp(point.x, minX, maxX);
    point.z = T.MathUtils.clamp(point.z, minZ, maxZ);
    return point;
  }

  function updateLanding() {
    const point = firstActive && actions.editable() && (grabbed || firstAsset) ? floorPoint(grabbed ?? null) : null;
    landing.visible = Boolean(point);
    if (point) landing.position.copy(point).setY(.016);
  }

  function firstClick(button) {
    if (!actions.editable()) {
      firstStatus('当前不可编辑摆位。', true);
      return;
    }
    if (button === 0 && !grabbed) {
      const hit = pick(undefined, false, false);
      if (!hit) {
        firstStatus('准星未命中实体。', true);
        return;
      }
      const id = hit.node.userData.entity.id;
      actions.select(id, false);
      if (!firstActive) return;
      grabbed = id;
      firstStatus('已抓取：对准地板后左键确认，R 取消。');
    } else if (button === 0) {
      const point = floorPoint(grabbed);
      if (!point) {
        firstStatus('请将准星指向可摆放的地板范围。', true);
        return;
      }
      const id = grabbed;
      grabbed = null;
      firstStatus('已结束抓取，可在表单中检查或提交摆位。');
      actions.move(id, point.x, point.z);
    } else {
      if (grabbed || !firstAsset) {
        firstStatus(grabbed ? '请先放下实体或按 R 取消抓取。' : '请先选择要投放的素材。', true);
        return;
      }
      const point = floorPoint();
      if (!point) {
        firstStatus('请将准星指向可投放的地板范围。', true);
        return;
      }
      actions.firstPersonAdd?.(firstAsset, [point.x, 0, point.z]);
    }
    redraw();
  }

  function pick(event, includeCamera = true, includeLift = true) {
    aim(event);
    models.updateMatrixWorld(true);
    const objects = models.children.slice();
    if (includeCamera && marker.visible && typeof actions.moveCamera === 'function') {
      marker.updateMatrixWorld(true);
      objects.push(cameraBody);
      if (arrow.visible) objects.push(arrow);
    }
    const hit = ray.intersectObjects(objects, true).find(item => includeLift || !item.object.userData.lift);
    if (!hit) return null;
    const node = hit.object.parent;
    if (node === marker || node === arrow) return { mode: 'camera', node: marker, point: hit.point };
    return { mode: hit.object.userData.lift ? 'lift' : 'entity', node, point: hit.point };
  }

  canvas.addEventListener('mousedown', event => {
    if (!firstActive || (event.button !== 0 && event.button !== 2)) return;
    event.preventDefault();
    firstClick(event.button);
  });
  canvas.addEventListener('pointerdown', event => {
    if (firstActive || firstPending || document.pointerLockElement === canvas || !documentModel || gesture ||
        event.button !== 0 || event.isPrimary === false) return;
    const hit = actions.editable() && pick(event);
    gesture = { pointer: event.pointerId, x: event.clientX, y: event.clientY };
    if (hit) {
      const id = hit.node.userData.entity?.id;
      const normal = hit.mode === 'lift' ? camera.position.clone().sub(hit.point).setY(0).normalize() : up;
      if (hit.mode === 'lift' && normal.lengthSq() === 0) normal.set(Math.sin(orbit.theta), 0, Math.cos(orbit.theta));
      gesture.mode = hit.mode;
      gesture.entity = id;
      gesture.plane = new T.Plane().setFromNormalAndCoplanarPoint(normal, hit.point);
      gesture.offset = hit.node.position.clone().sub(hit.point);
      if (id !== undefined) actions.select(id, false);
    }
    canvas.setPointerCapture(event.pointerId);
  });

  canvas.addEventListener('pointermove', event => {
    if (firstActive || firstPending) return;
    if (!gesture) {
      const hit = documentModel && pick(event);
      canvas.title = hit?.node.name || '';
      return;
    }
    if (gesture.pointer !== event.pointerId) return;
    const dx = event.clientX - gesture.x, dy = event.clientY - gesture.y;
    if (gesture.mode) {
      if (!actions.editable() || (!gesture.moved && Math.hypot(dx, dy) < 3)) return;
      gesture.moved = true;
      aim(event);
      const point = ray.ray.intersectPlane(gesture.plane, new T.Vector3());
      if (!point) return;
      point.add(gesture.offset);
      if (gesture.mode === 'lift') actions.lift(gesture.entity, Math.max(0, point.y));
      else if (gesture.mode === 'camera') actions.moveCamera(point.x, point.z);
      else actions.move(gesture.entity, point.x, point.z);
    } else {
      orbit.theta -= dx * .008;
      orbit.phi = T.MathUtils.clamp(orbit.phi - dy * .008, .16, 1.49);
      gesture.x = event.clientX;
      gesture.y = event.clientY;
      redraw();
    }
  });
  canvas.addEventListener('pointerleave', () => { canvas.title = ''; });
  canvas.addEventListener('lostpointercapture', () => { gesture = null; });
  canvas.addEventListener('contextmenu', event => {
    event.preventDefault();
    if (firstActive || firstPending || !actions.editable()) return;
    const hit = pick(event, false);
    if (hit) actions.select(hit.node.userData.entity.id, true);
  });
  canvas.addEventListener('wheel', event => {
    if (!documentModel) return;
    event.preventDefault();
    if (firstActive || firstPending || gesture?.mode) return;
    const units = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? host.clientHeight : 1;
    orbit.radius = T.MathUtils.clamp(orbit.radius * Math.exp(event.deltaY * units * .0015), .7, maxDistance);
    redraw();
  }, { passive: false });

  function dragAsset(kind) {
    assetKind = kind;
    if (!kind) host.removeAttribute('data-drop');
  }
  host.addEventListener('dragover', event => {
    if (failed || firstActive || firstPending || !assetKind || !actions.editable()) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = 'copy';
    host.setAttribute('data-drop', '');
  });
  host.addEventListener('dragleave', () => host.removeAttribute('data-drop'));
  host.addEventListener('drop', event => {
    const kind = assetKind;
    dragAsset(null);
    host.removeAttribute('data-drop');
    if (failed || firstActive || firstPending || !kind || !actions.editable()) return;
    event.preventDefault();
    aim(event);
    const point = ray.ray.intersectPlane(floorPlane, new T.Vector3());
    if (!point || Math.abs(point.x) > ground[0] / 2 || Math.abs(point.z) > ground[1] / 2) {
      actions.missedFloor();
      return;
    }
    actions.add(kind, [point.x, 0, point.z]);
  });

  const observer = new ResizeObserver(redraw);
  canvas.addEventListener('webglcontextlost', () => fail(new Error('WebGL 上下文已丢失')));
  host.append(canvas);
  observer.observe(host);
  redraw();
  return { load, playPose, setGround, update: updateDrafts, updateDrafts, fit, cancel, dragAsset,
    enterFirstPerson, exitFirstPerson, useFirstPersonCamera, setFirstPersonAsset };
};
