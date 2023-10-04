// Función para obtener el mensaje según la opción seleccionada
function getWhatsAppMessage() {
    var subscriptionType = document.querySelector('input[name="subscription-type"]:checked');
    
    if (subscriptionType) {
        if (subscriptionType.value === "mensual") {
            return "¡Hola! Estoy interesado en la suscripción de un mes por $7.";
        } else if (subscriptionType.value === "mensual") {
            return "¡Hola! Estoy interesado en la suscripción mensual de $4.";
        }
    }

    // Por defecto, si no se selecciona ninguna opción, enviar un mensaje genérico
    return "¡Hola! Estoy interesado en una suscripción.";
}

// Función para manejar el clic en el enlace de WhatsApp
function handleWhatsAppClick(event) {
    event.preventDefault(); // Evitar el comportamiento predeterminado del enlace
    var message = encodeURIComponent(getWhatsAppMessage());
    var whatsappURL = "https://wa.me/5492216424205?text=" + message;
    window.open(whatsappURL, "_blank");
}

// Agregar un evento de clic al enlace de WhatsApp
var whatsappLink = document.querySelector('a[aria-label="Chat on WhatsApp"]');
whatsappLink.addEventListener("click", handleWhatsAppClick);
