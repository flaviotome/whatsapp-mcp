const errors = {
  1: "Senha incorreta.",
  wait: "Muitas tentativas erradas. Espere alguns minutos e tente de novo.",
  origin: "Não foi possível validar a origem do pedido. Recarregue a página e tente de novo.",
};
const code = new URLSearchParams(location.search).get("e");
if (code && errors[code]) {
  const box = document.getElementById("error");
  box.textContent = errors[code];
  box.hidden = false;
}
