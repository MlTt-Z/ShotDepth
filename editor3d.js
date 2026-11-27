'use strict';

window.createSpikeMarkStage = function (host, actions) {
  const T = window.THREE;
  if (!T) throw new Error('未加载本地 Three.js');
  const renderer = new T.WebGLRenderer({ antialias: true });
  const canvas = renderer.domElement;
  canvas.setAttribute('aria-label', '三维摆位视窗');
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
  scene.add(room, models, marker);
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

  function cancel() {
    const pointer = gesture?.pointer;
    gesture = null;
    if (pointer !== undefined && canvas.hasPointerCapture(pointer)) canvas.releasePointerCapture(pointer);
    assetKind = null;
    host.removeAttribute('data-drop');
  }

  function fail(error) {
    if (failed) return;
    failed = true;
    cancel();
    cancelAnimationFrame(frame);
    observer.disconnect();
    canvas.remove();
    renderer.dispose();
    actions.unavailable(error);
  }

  function redraw() {
    if (frame || failed) return;
    frame = requestAnimationFrame(() => {
      frame = 0;
      try {
        const { clientWidth: width, clientHeight: height } = host;
        if (!width || !height) return;
        if (camera.aspect !== width / height || canvas.width !== Math.floor(width * renderer.getPixelRatio()) ||
            canvas.height !== Math.floor(height * renderer.getPixelRatio())) {
          camera.aspect = width / height;
          camera.updateProjectionMatrix();
          renderer.setSize(width, height, false);
        }
        view();
        renderer.render(scene, camera);
      } catch (error) { fail(error); }
    });
  }

  function view() {
    camera.position.setFromSpherical(orbit).add(target);
    camera.lookAt(target);
    camera.updateMatrixWorld();
  }

  function fit() {
    if (!documentModel) return;
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
    mesh.position.copy(start).addScaledVector(direction, .5);
    if (foot) {
      mesh.position.y -= r * .15;
    } else if (half) mesh.quaternion.setFromUnitVectors(up, direction.normalize());
    return mesh;
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
          parts.forEach(([a, b, radius], index) => node.add(mannequinPart(a, b, radius, index)));
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
      marker.visible = true;
      updateDrafts(new Map(), '');
      if (reset) fit();
    } catch (error) { fail(error); }
  }

  function updateDrafts(drafts, selected, cameraDraft) {
    if (failed || !documentModel) return;
    for (const [id, node] of nodes) {
      const { transform } = node.userData.entity;
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

  function aim(event) {
    const rect = canvas.getBoundingClientRect();
    view();
    ray.setFromCamera({ x: 2 * (event.clientX - rect.left) / rect.width - 1,
      y: 1 - 2 * (event.clientY - rect.top) / rect.height }, camera);
  }

  function pick(event, includeCamera = true) {
    aim(event);
    models.updateMatrixWorld(true);
    const objects = models.children.slice();
    if (includeCamera && marker.visible && typeof actions.moveCamera === 'function') {
      marker.updateMatrixWorld(true);
      objects.push(cameraBody);
      if (arrow.visible) objects.push(arrow);
    }
    const hit = ray.intersectObjects(objects, true)[0];
    if (!hit) return null;
    const node = hit.object.parent;
    if (node === marker || node === arrow) return { mode: 'camera', node: marker, point: hit.point };
    return { mode: hit.object.userData.lift ? 'lift' : 'entity', node, point: hit.point };
  }

  canvas.addEventListener('pointerdown', event => {
    if (!documentModel || gesture || event.button !== 0 || event.isPrimary === false) return;
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
    if (!actions.editable()) return;
    const hit = pick(event, false);
    if (hit) actions.select(hit.node.userData.entity.id, true);
  });
  canvas.addEventListener('wheel', event => {
    if (!documentModel) return;
    event.preventDefault();
    if (gesture?.mode) return;
    const units = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? host.clientHeight : 1;
    orbit.radius = T.MathUtils.clamp(orbit.radius * Math.exp(event.deltaY * units * .0015), .7, maxDistance);
    redraw();
  }, { passive: false });

  function dragAsset(kind) {
    assetKind = kind;
    if (!kind) host.removeAttribute('data-drop');
  }
  host.addEventListener('dragover', event => {
    if (failed || !assetKind || !actions.editable()) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = 'copy';
    host.setAttribute('data-drop', '');
  });
  host.addEventListener('dragleave', () => host.removeAttribute('data-drop'));
  host.addEventListener('drop', event => {
    const kind = assetKind;
    dragAsset(null);
    host.removeAttribute('data-drop');
    if (failed || !kind || !actions.editable()) return;
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
  return { load, setGround, update: updateDrafts, updateDrafts, fit, cancel, dragAsset };
};
